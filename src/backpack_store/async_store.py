"""Bounded asynchronous admission to one thread-owned application store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import inspect
import os
from queue import Empty, Full, Queue
import threading
from typing import Generic, TypeVar

from .errors import BackpackError, StorageBusyError, StorageError, StoreClosedError, TransactionError, ValidationError
from .relational import ApplicationStore

T = TypeVar("T")
R = TypeVar("R")


class OperationReceipt(Generic[R]):
    """An accepted operation's eventual result, retained across caller cancellation."""

    def __init__(self, future: asyncio.Future[R]):
        self._future = future
        # A discarded receipt must not produce an unobserved-exception warning.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    @property
    def done(self) -> bool:
        return self._future.done()

    async def wait(self) -> R:
        return await asyncio.shield(self._future)


class DispatchedCancellation(asyncio.CancelledError):
    """Cancellation after dispatch has an outcome available through the receipt."""

    def __init__(self, receipt: OperationReceipt):
        super().__init__("storage operation was dispatched; await its receipt or reload to reconcile")
        self.receipt = receipt


@dataclass
class _Work:
    operation: Callable
    transactional: bool
    future: Future


class AsyncApplicationStore(Generic[T]):
    """One event-loop owner, one bounded queue, one SQLite-owning worker thread."""

    def __init__(self, path, configure, timeout, queue_limit):
        self._loop = asyncio.get_running_loop()
        self._pid = os.getpid()
        self._queue: Queue[_Work] = Queue(maxsize=queue_limit)
        self._closing = threading.Event()
        self._admission = threading.Lock()
        self._ready: Future = Future()
        self._closed: Future = Future()
        self._thread = threading.Thread(target=self._worker, args=(path, configure, timeout), name="application-storage")

    @classmethod
    async def open(cls, path, *, configure: Callable[[ApplicationStore], T],
                   timeout: float = 5.0, queue_limit: int = 64) -> AsyncApplicationStore[T]:
        if type(queue_limit) is not int or not 1 <= queue_limit <= 1024:
            raise ValidationError("async queue limit must be between 1 and 1024")
        if not callable(configure) or inspect.iscoroutinefunction(configure):
            raise ValidationError("async storage requires a synchronous configuration callback")
        instance = cls(path, configure, timeout, queue_limit)
        ready = OperationReceipt(asyncio.wrap_future(instance._ready))
        instance._thread.start()
        try:
            await ready.wait()
        except BaseException:
            instance._closing.set()
            await instance.aclose()
            raise
        return instance

    @staticmethod
    def _synchronous(callback, value):
        result = callback(value)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ValidationError("storage callbacks must return synchronously")
        return result

    def _worker(self, path, configure, timeout):
        store = None
        try:
            store = ApplicationStore.open(path, timeout=timeout)
            state = self._synchronous(configure, store)
            self._ready.set_result(None)
            while True:
                try:
                    work = self._queue.get(timeout=0.05)
                except Empty:
                    if self._closing.is_set():
                        break
                    continue
                try:
                    if not work.future.set_running_or_notify_cancel():
                        continue
                    try:
                        if work.transactional:
                            with store.unit_of_work():
                                result = self._synchronous(work.operation, state)
                        else:
                            result = self._synchronous(work.operation, state)
                    except BackpackError as error:
                        work.future.set_exception(error)
                    except BaseException:
                        work.future.set_exception(TransactionError("asynchronous storage callback failed"))
                    else:
                        work.future.set_result(result)
                finally:
                    self._queue.task_done()
        except BaseException as error:
            failure = error if isinstance(error, BackpackError) else StorageError("asynchronous storage startup failed")
            if not self._ready.done():
                self._ready.set_exception(failure)
        finally:
            with self._admission:
                self._closing.set()
                while True:
                    try:
                        pending = self._queue.get_nowait()
                    except Empty:
                        break
                    if pending.future.set_running_or_notify_cancel():
                        pending.future.set_exception(StorageError("storage worker stopped before dispatch"))
                    self._queue.task_done()
            try:
                if store is not None:
                    store.close()
            except BaseException:
                self._closed.set_exception(StorageError("asynchronous storage cleanup failed"))
            else:
                self._closed.set_result(None)

    def _check_owner(self):
        if os.getpid() != self._pid or asyncio.get_running_loop() is not self._loop:
            raise StorageError("async storage must stay in its opening process and event loop")

    async def run(self, operation: Callable[[T], R], *, transactional: bool = False) -> R:
        """Execute a synchronous callback on configured worker-owned state."""
        self._check_owner()
        if not callable(operation) or inspect.iscoroutinefunction(operation) or type(transactional) is not bool:
            raise ValidationError("async storage requires a synchronous callback and explicit transaction flag")
        future: Future[R] = Future()
        receipt = OperationReceipt(asyncio.wrap_future(future))
        try:
            with self._admission:
                if self._closing.is_set():
                    future.cancel()
                    raise StoreClosedError("async storage is closing or closed")
                self._queue.put_nowait(_Work(operation, transactional, future))
        except Full:
            future.cancel()
            raise StorageBusyError("async storage queue is full; retry with application backpressure") from None
        try:
            return await receipt.wait()
        except asyncio.CancelledError:
            if future.cancel():
                raise
            raise DispatchedCancellation(receipt) from None

    async def aclose(self) -> None:
        """Stop admission, drain accepted work and close on the owning thread."""
        self._check_owner()
        with self._admission:
            self._closing.set()
        await OperationReceipt(asyncio.wrap_future(self._closed)).wait()
        while self._thread.is_alive():
            await asyncio.sleep(0)

    async def __aenter__(self):
        self._check_owner()
        if self._closing.is_set():
            raise StoreClosedError("async storage is closing or closed")
        return self

    async def __aexit__(self, *_):
        await self.aclose()
