"""Spawned-process acceptance for one-host SQLite ownership and recovery."""

import asyncio
from contextlib import closing
from dataclasses import dataclass, replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from backpack_store import (
    AlreadyExistsError, ApplicationStore, AsyncApplicationStore, DataclassCodec,
    DispatchedCancellation, MigrationStep, RevisionConflictError, StorageError,
    migrate_sqlite,
)


@dataclass
class Item:
    key: str
    title: str


def configure(store):
    codec_type = DataclassCodec
    if os.environ.get("BACKPACK_PROCESS_CODEC") == "enforce-type":
        from backpack_store.enforce_type_codec import EnforceTypeCodec
        codec_type = EnforceTypeCodec
    items = store.register(codec_type(Item, type_key="process.item"), namespace="process",
                           table="app_items", primary_key="key", columns={"key": "key", "title": "title"})
    store.initialize("process")
    return items


def startup_worker(path, start, read, updated, output):
    try:
        start.wait(20)
        with ApplicationStore.open(path, timeout=10) as store:
            items = configure(store)
            created = False
            try:
                items.create(Item("shared", "Initial"))
                created = True
            except AlreadyExistsError:
                pass
            row = items.get("shared")
            read.wait(20)
            won = False
            try:
                items.replace("shared", Item("shared", "Winner"), expected_revision=row.revision)
                won = True
            except RevisionConflictError:
                pass
            updated.wait(20)
            actual = items.get("shared")
            connection = store._connection()
            pragmas = [connection.execute("PRAGMA " + name).fetchone()[0]
                       for name in ("journal_mode", "synchronous", "foreign_keys")]
            output.put(("ok", created, won, actual.revision, actual.value.title, pragmas))
    except BaseException as error:
        output.put(("error", type(error).__name__, str(error)))


def initial_schema(connection):
    connection.execute("CREATE TABLE documents (key TEXT PRIMARY KEY, title TEXT)")


def upgrade_schema(connection):
    connection.execute("ALTER TABLE documents ADD COLUMN rank INTEGER")


def migration_worker(path, backups, start, output):
    try:
        start.wait(20)
        result = migrate_sqlite(path, namespace="documents", backup_directory=backups, timeout=10,
                                steps=[MigrationStep(1, "documents", initial_schema),
                                       MigrationStep(2, "document-ranks", upgrade_schema)])
        output.put(("ok", result.version, result.backup is not None))
    except BaseException as error:
        output.put(("error", type(error).__name__, str(error)))


def crash_worker(path):
    with ApplicationStore.open(path) as store:
        items = configure(store)
        with store.unit_of_work():
            items.replace("shared", Item("shared", "Uncommitted"), expected_revision="1")
            items.create(Item("retry", "Uncommitted reservation"))
            os._exit(23)


def locked_worker(path, output):
    try:
        with ApplicationStore.open(path, timeout=0.05):
            output.put("unexpected success")
    except StorageError:
        output.put("bounded lock refusal")


def cancellation_worker(path, entered, release, output):
    async def exercise():
        facade = await AsyncApplicationStore.open(path, configure=configure)
        try:
            def update(items):
                def transform(value):
                    entered.set()
                    if not release.wait(20):
                        raise TimeoutError
                    return replace(value, title="Committed after cancellation")
                return items.update("shared", expected_revision="1", transform=transform)
            pending = asyncio.create_task(facade.run(update))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            pending.cancel()
            try:
                await pending
            except DispatchedCancellation as error:
                row = await error.receipt.wait()
                output.put(("ok", row.revision, row.value.title))
        finally:
            await facade.aclose()
        if facade._thread.is_alive():
            raise AssertionError("owned worker was not stopped")
    try:
        asyncio.run(exercise())
    except BaseException as error:
        output.put(("error", type(error).__name__, str(error)))


class SQLiteProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "application.sqlite"
        self.backups = Path(self.directory.name) / "backups"
        self.context = multiprocessing.get_context("spawn")

    def run_workers(self, target, arguments, count=4):
        output = self.context.Queue()
        processes = [self.context.Process(target=target, args=(*arguments, output)) for _ in range(count)]
        try:
            for process in processes:
                process.start()
            results = [output.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all(result[0] == "ok" for result in results), results)
            return results
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(10)
            output.close()
            output.join_thread()

    def test_four_process_startup_checked_writes_visibility_and_restart(self):
        results = self.run_workers(startup_worker,
            (self.path, self.context.Barrier(4), self.context.Barrier(4), self.context.Barrier(4)))
        self.assertEqual(sum(result[1] for result in results), 1)
        self.assertEqual(sum(result[2] for result in results), 1)
        self.assertTrue(all(result[3:5] == ("2", "Winner") for result in results), results)
        self.assertTrue(all(result[5] == ["wal", 2, 1] for result in results), results)
        with ApplicationStore.open(self.path) as store:
            self.assertEqual(configure(store).get("shared").revision, "2")

    def test_four_process_fresh_migration_applies_each_step_once(self):
        results = self.run_workers(migration_worker, (self.path, self.backups, self.context.Barrier(4)))
        self.assertTrue(all(result[1:] == (2, False) for result in results), results)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT version,name FROM bp_schema_migrations ORDER BY version").fetchall(),
                             [(1, "documents"), (2, "document-ranks")])

    def test_four_process_upgrade_creates_one_backup_and_preserves_rows(self):
        migrate_sqlite(self.path, namespace="documents", backup_directory=self.backups,
                       steps=[MigrationStep(1, "documents", initial_schema)])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO documents VALUES ('a','Retained')")
        results = self.run_workers(migration_worker, (self.path, self.backups, self.context.Barrier(4)))
        self.assertEqual(sum(result[2] for result in results), 1)
        self.assertEqual(len(list(self.backups.glob("*.sqlite3"))), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM documents").fetchall(), [("a", "Retained", None)])

    def test_worker_exit_rolls_back_rows_and_key_reservations(self):
        with ApplicationStore.open(self.path) as store:
            configure(store).create(Item("shared", "Initial"))
        process = self.context.Process(target=crash_worker, args=(self.path,))
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
        self.assertEqual(process.exitcode, 23)
        with ApplicationStore.open(self.path) as store:
            items = configure(store)
            self.assertEqual(items.get("shared").value.title, "Initial")
            self.assertEqual(items.get("shared").revision, "1")
            items.create(Item("retry", "Reservation rolled back"))

    def test_independent_process_lock_wait_is_bounded(self):
        with ApplicationStore.open(self.path) as store:
            configure(store)
            with store.unit_of_work():
                output = self.context.Queue()
                process = self.context.Process(target=locked_worker, args=(self.path, output))
                try:
                    process.start()
                    self.assertEqual(output.get(timeout=20), "bounded lock refusal")
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(10)
                    output.close()
                    output.join_thread()

    def test_cancelled_caller_in_separate_process_reconciles_commit_and_closes(self):
        with ApplicationStore.open(self.path) as store:
            configure(store).create(Item("shared", "Initial"))
        entered, release = self.context.Event(), self.context.Event()
        output = self.context.Queue()
        process = self.context.Process(target=cancellation_worker, args=(self.path, entered, release, output))
        try:
            process.start()
            self.assertTrue(entered.wait(20))
            with closing(sqlite3.connect(self.path)) as connection:
                self.assertEqual(connection.execute("SELECT title FROM app_items").fetchone()[0], "Initial")
            release.set()
            self.assertEqual(output.get(timeout=20), ("ok", "2", "Committed after cancellation"))
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        finally:
            release.set()
            if process.is_alive():
                process.terminate()
            process.join(10)
            output.close()
            output.join_thread()
        with ApplicationStore.open(self.path) as store:
            self.assertEqual(configure(store).get("shared").revision, "2")
