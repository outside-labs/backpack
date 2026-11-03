import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest

from backpack_store import (
    ApplicationStore, AsyncApplicationStore, DispatchedCancellation, NotFoundError,
    StorageBusyError, StoreClosedError, TransactionError,
)
from test_relational import WorkItem, register


def configure(store):
    projects, items = register(store)
    store.initialize("garden")
    return {"projects": projects, "items": items}


async def wait_event(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 5)


class AsyncApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "application.sqlite"
        self.store = await AsyncApplicationStore.open(self.path, configure=configure, queue_limit=1)
        self.addAsyncCleanup(self.store.aclose)

    async def test_concurrent_callers_use_one_owned_thread_and_detached_rows(self):
        thread_ids = []
        def create(state):
            thread_ids.append(threading.get_ident())
            return state["items"].create(WorkItem("a", "One"))
        row = await self.store.run(create)
        row.value.title = "unsaved"
        result = await self.store.run(lambda state: state["items"].get("a"))
        self.assertEqual(result.value.title, "One")
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        await self.store.run(lambda state: thread_ids.append(threading.get_ident()))
        self.assertEqual(len(set(thread_ids)), 1)
        await self.store.aclose()
        with ApplicationStore.open(self.path) as reopened:
            _, items = register(reopened)
            reopened.initialize("garden")
            self.assertEqual(items.get("a"), result)

    async def test_queued_cancellation_never_dispatches_and_capacity_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block(state):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
        active = asyncio.create_task(self.store.run(block))
        await wait_event(entered)
        queued = asyncio.create_task(self.store.run(lambda state: state["items"].create(WorkItem("a", "Skipped"))))
        await asyncio.sleep(0)
        with self.assertRaises(StorageBusyError):
            await self.store.run(lambda state: None)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await queued
        self.assertNotIsInstance(caught.exception, DispatchedCancellation)
        release.set()
        await active
        await self.store.aclose()
        with ApplicationStore.open(self.path) as reopened:
            _, items = register(reopened)
            reopened.initialize("garden")
            with self.assertRaises(NotFoundError):
                items.get("a")

    async def test_cancel_during_write_retains_committed_receipt(self):
        await self.store.run(lambda state: state["items"].create(WorkItem("a", "One")))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def update(state):
            def transform(value):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError
                return replace(value, title="Committed")
            return state["items"].update("a", expected_revision="1", transform=transform)
        task = asyncio.create_task(self.store.run(update))
        await wait_event(entered)
        task.cancel()
        with self.assertRaises(DispatchedCancellation) as caught:
            await task
        receipt = caught.exception.receipt
        release.set()
        result = await receipt.wait()
        self.assertEqual((result.revision, result.value.title), ("2", "Committed"))
        self.assertEqual(await self.store.run(lambda state: state["items"].get("a")), result)

    async def test_cancel_after_commit_does_not_report_a_rollback(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def commit_then_wait(state):
            row = state["items"].create(WorkItem("a", "Committed"))
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return row
        task = asyncio.create_task(self.store.run(commit_then_wait))
        await wait_event(entered)
        task.cancel()
        with self.assertRaises(DispatchedCancellation) as caught:
            await task
        release.set()
        self.assertEqual((await caught.exception.receipt.wait()).revision, "1")

    async def test_close_drains_work_refuses_admission_and_survives_cancelled_waiter(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def block(state):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return state["items"].create(WorkItem("a", "One"))
        active = asyncio.create_task(self.store.run(block))
        await wait_event(entered)
        queued = asyncio.create_task(self.store.run(lambda state: state["items"].create(WorkItem("b", "Two"))))
        await asyncio.sleep(0)
        closing = asyncio.create_task(self.store.aclose())
        await asyncio.sleep(0)
        with self.assertRaises(StoreClosedError):
            await self.store.run(lambda state: None)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        release.set()
        await asyncio.gather(active, queued)
        await self.store.aclose()
        self.assertFalse(self.store._thread.is_alive())

    async def test_unit_of_work_failure_rolls_back_and_worker_remains_usable(self):
        def failing(state):
            state["items"].create(WorkItem("a", "One"))
            raise ValueError("private callback value")
        with self.assertRaises(TransactionError) as caught:
            await self.store.run(failing, transactional=True)
        self.assertNotIn("private callback value", str(caught.exception))
        self.assertEqual(await self.store.run(lambda state: state["items"].list()), ())
        await self.store.run(lambda state: state["items"].create(WorkItem("a", "Retry")))

    async def test_startup_failure_and_cancellation_close_the_owned_connection(self):
        def failing(store):
            raise ValueError("private startup value")
        with self.assertRaises(Exception) as caught:
            await AsyncApplicationStore.open(self.path, configure=failing)
        self.assertNotIn("private startup value", str(caught.exception))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(store):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return configure(store)
        opening = asyncio.create_task(AsyncApplicationStore.open(self.path, configure=slow))
        await wait_event(entered)
        opening.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await opening
