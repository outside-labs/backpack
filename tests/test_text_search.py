import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from backpack_store import ApplicationStore, AsyncApplicationStore, DispatchedCancellation, Filter, InvalidCursorError, Order, QueryBudgetError, StorageError, TextSearch, ValidationError
from test_relational import ApplicationFixture, WorkItem, register


class TextSearchTests(ApplicationFixture, unittest.TestCase):
    def test_literal_unicode_empty_search_and_predicate_intersection(self):
        values = ["100%_\\' OR 1=1;--", "café", "cafe\u0301", "CAFÉ", "a\x00b", "café"]
        for n, title in enumerate(values):
            self.items.create(WorkItem(str(n), title, done=n == 5))
        for term, expected in [("%_\\'", ["0"]), ("café", ["1", "5"]), ("CAFÉ", ["3"]), ("\x00", ["4"]), ("", [str(n) for n in range(6)])]:
            page = self.items.find(search=TextSearch(["title"], term))
            self.assertEqual([r.key for r in page.rows], expected)
        page = self.items.find(search=TextSearch(["title", "key"], "café"), filters=[Filter("done", "eq", True)])
        self.assertEqual([r.key for r in page.rows], ["5"])
        self.assertEqual([r.key for r in self.items.find(search=TextSearch(["key", "project_key"], "2")).rows], ["2"])

    def test_query_cursor_field_set_empty_normalization_and_reopen(self):
        for key in ("a", "b", "c"):
            self.items.create(WorkItem(key, "same"))
        first = self.items.find(search=TextSearch(["title", "key"], "same"), order=[Order("rank")], limit=1)
        rest = self.items.find(search=TextSearch(["key", "title"], "same"), order=[Order("rank")], cursor=first.next_cursor)
        self.assertEqual([r.key for r in first.rows + rest.rows], ["a", "b", "c"])
        for search in [TextSearch(["title"], "same"), TextSearch(["key", "title"], "other"), None]:
            with self.assertRaises(InvalidCursorError):
                self.items.find(search=search, order=[Order("rank")], cursor=first.next_cursor)
        empty = self.items.find(search=TextSearch(["title"], ""), limit=1)
        self.assertEqual(self.items.find(cursor=empty.next_cursor).rows[0].key, "b")
        self.store.close()
        with ApplicationStore.open(self.path) as store:
            _, items = register(store)
            store.initialize("garden")
            self.assertEqual([r.key for r in items.find(search=TextSearch(["title"], "same")).rows], ["a", "b", "c"])

    def test_invalid_descriptors_and_work_exhaustion_preserve_connection(self):
        for fields, term in [([], "x"), (["title"] * 2, "x"), (["a"] * 9, "x"), ([1], "x"), (["title"], "x" * 257), (["title"], 1)]:
            with self.assertRaises(ValidationError):
                TextSearch(fields, term)
        for search in [TextSearch(["done"], "x"), TextSearch(["missing"], "x"), {"fields": ["title"], "term": "x"}]:
            with self.assertRaises(ValidationError):
                self.items.find(search=search)
        self.items.create(WorkItem("a", "private text"))
        with self.assertRaises(QueryBudgetError) as caught:
            self.items.find(search=TextSearch(["title"], "private"), max_work=1)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.items.find().rows[0].revision, "1")

    def test_other_process_writes_are_visible_to_fresh_query(self):
        self.items.create(WorkItem("a", "before"))
        code = 'from backpack_store import ApplicationStore; from test_relational import WorkItem,register; import sys; s=ApplicationStore.open(sys.argv[1]); _,m=register(s); s.initialize("garden"); m.replace("a",WorkItem("a","after"),expected_revision="1"); s.close()'
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).parent), str(Path(__file__).parents[1] / 'src')]))
        subprocess.run([sys.executable, "-c", code, str(self.path)], check=True, env=env, capture_output=True)
        row = self.items.find(search=TextSearch(["title"], "after")).rows[0]
        self.assertEqual((row.key, row.revision), ("a", "2"))

    def test_work_exhaustion_during_trusted_decode_keeps_budget_classification(self):
        self.items.create(WorkItem("a", "private value"))
        connection = self.store._connection()
        original = self.items._codec.decode
        def decoding(payload):
            # A trusted codec can perform SQLite work while a row is hydrated.
            # The row validator redacts the interruption; find retains its budget.
            connection.execute("WITH RECURSIVE steps(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM steps WHERE n<10000) SELECT sum(n) FROM steps").fetchone()
            return original(payload)
        with patch.object(type(self.items._codec), "decode", side_effect=decoding):
            with self.assertRaises(QueryBudgetError) as caught:
                self.items.find(max_work=1000)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.items.find().rows[0].revision, "1")
        with patch.object(type(self.items._codec), "decode", side_effect=ValueError("private codec failure")):
            with self.assertRaises(StorageError) as caught:
                self.items.find()
            self.assertNotIsInstance(caught.exception, QueryBudgetError)


class AsyncSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatched_cancel_has_detached_receipt_and_worker_drains(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            def configure(store):
                _, items = register(store)
                store.initialize("garden")
                items.create(WorkItem("a", "same"))
                return items
            worker = await AsyncApplicationStore.open(Path(directory) / "db", configure=configure)
            entered, release = threading.Event(), threading.Event()
            def query(model):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError
                return model.find(search=TextSearch(["title"], "same"))
            try:
                task = asyncio.create_task(worker.run(query))
                while not entered.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                with self.assertRaises(DispatchedCancellation) as caught:
                    await task
                release.set()
                page = await caught.exception.receipt.wait()
                self.assertEqual(page.rows[0].key, "a")
                page.rows[0].value.title = "detached"
                self.assertEqual((await worker.run(lambda m: m.get("a"))).value.title, "same")
            finally:
                release.set()
                await worker.aclose()
            self.assertFalse(worker._thread.is_alive())
