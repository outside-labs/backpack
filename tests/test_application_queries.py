import base64
import json
import unittest

from backpack_store import Filter, InvalidCursorError, Order, ValidationError
from test_relational import ApplicationFixture, WorkItem


class ApplicationQueryTests(ApplicationFixture, unittest.TestCase):
    def populate(self):
        for key, rank, done, estimate in (("a", 2, False, 1.0), ("b", None, True, 2.0),
                                          ("c", 1, False, None), ("d", 2, True, 1.0),
                                          ("e", None, False, None), ("f", 1, True, 3.0)):
            self.items.create(WorkItem(key, "Garden", done, rank=rank, estimate=estimate))

    def pages(self, **query):
        result, cursor = [], None
        while True:
            page = self.items.find(**query, limit=1, cursor=cursor)
            result.extend(row.key for row in page.rows)
            cursor = page.next_cursor
            if cursor is None:
                return result
            self.assertLessEqual(len(cursor), 16_384)
            self.assertLessEqual(len(result), 10)

    def test_null_last_equal_values_and_mixed_direction_keyset_pages(self):
        self.populate()
        self.assertEqual(self.pages(order=[Order("rank")]), ["c", "f", "a", "d", "b", "e"])
        self.assertEqual(self.pages(order=[Order("rank", True)]), ["a", "d", "c", "f", "b", "e"])
        self.assertEqual(self.pages(order=[Order("done", True), Order("rank")]), ["f", "d", "b", "c", "a", "e"])
        self.assertEqual(self.pages(order=[Order("key", True)]), ["f", "e", "d", "c", "b", "a"])
        self.assertEqual(self.pages(), ["a", "b", "c", "d", "e", "f"])

    def test_predicates_use_typed_sql_null_semantics(self):
        self.populate()
        for operation, value, expected in (("eq", 2, ["a", "d"]), ("ne", 2, ["c", "f"]),
                                            ("lt", 2, ["c", "f"]), ("le", 2, ["a", "c", "d", "f"]),
                                            ("gt", 1, ["a", "d"]), ("ge", 2, ["a", "d"]),
                                            ("in", [1, 2], ["a", "c", "d", "f"]),
                                            ("is_null", True, ["b", "e"]),
                                            ("is_null", False, ["a", "c", "d", "f"])):
            with self.subTest(operation=operation):
                self.assertEqual(self.pages(filters=[Filter("rank", operation, value)]), expected)
        self.assertEqual(self.pages(filters=[Filter("done", "eq", True), Filter("rank", "ge", 2)]), ["d"])
        self.assertEqual(self.pages(filters=[Filter("estimate", "in", [1.0, 3.0])]), ["a", "d", "f"])

    def test_string_order_and_injection_values_are_bound_data(self):
        for key, title in (("a", "é"), ("b", "Z"), ("c", "a"), ("d", "' OR 1=1; DROP TABLE app_work_items;--")):
            self.items.create(WorkItem(key, title))
        self.assertEqual(self.pages(order=[Order("title")]), ["d", "b", "c", "a"])
        page = self.items.find(filters=[Filter("title", "eq", "' OR 1=1; DROP TABLE app_work_items;--")])
        self.assertEqual([row.key for row in page.rows], ["d"])
        self.assertEqual(len(self.items.list()), 4)

    def test_invalid_queries_fail_before_sql(self):
        statements = []
        self.store._connection().set_trace_callback(statements.append)
        invalid = [dict(filters=[Filter("unknown", "eq", 1)]), dict(filters=[Filter("rank", "like", "x")]),
                   dict(filters=[Filter("rank", "eq", True)]), dict(filters=[Filter("done", "gt", False)]),
                   dict(filters=[Filter("rank", "eq", None)]), dict(filters=[Filter("rank", "in", [None])]),
                   dict(filters=[Filter("rank", "in", [])]), dict(filters=[Filter("rank", "in", list(range(101)))]),
                   dict(filters=[Filter("rank", "is_null", 1)]), dict(filters=[Filter("estimate", "eq", float("nan"))]),
                   dict(filters=[Filter("rank", "eq", 1)] * 17), dict(order=[Order("rank"), Order("rank")]),
                   dict(order=[Order("key")] * 4), dict(order=[Order("missing")]), dict(limit=True), dict(limit=1001)]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValidationError):
                self.items.find(**query)
        self.assertEqual(statements, [])

    def test_cursor_binds_query_and_schema_and_rejects_malformed_tuples(self):
        self.populate()
        first = self.items.find(order=[Order("rank")], limit=1)
        cursor = first.next_cursor
        self.assertIsNotNone(cursor)
        for query in (dict(order=[Order("rank", True)]), dict(filters=[Filter("done", "eq", True)]), {}):
            with self.assertRaises(InvalidCursorError):
                self.items.find(**query, cursor=cursor)
        data = json.loads(base64.urlsafe_b64decode(cursor))
        for last in ([None, None], [True, "c"], [1], [1, "invalid key"]):
            tampered = base64.urlsafe_b64encode(json.dumps(data | {"last": last}).encode()).decode()
            with self.assertRaises(InvalidCursorError):
                self.items.find(order=[Order("rank")], cursor=tampered)
        for malformed in ("bad", "!", "x" * 16_385, "", 123):
            with self.assertRaises(InvalidCursorError):
                self.items.find(order=[Order("rank")], cursor=malformed)
        schema = self.store._schemas["garden"]
        self.store._schemas["garden"] = (2, "different-signature")
        with self.assertRaises(InvalidCursorError):
            self.items.find(order=[Order("rank")], cursor=cursor)
        self.store._schemas["garden"] = schema

    def test_canonical_filter_order_and_page_size_do_not_change_query_identity(self):
        self.populate()
        predicates = [Filter("done", "eq", False), Filter("rank", "in", [2, 1, 2])]
        page = self.items.find(filters=predicates, limit=1)
        next_page = self.items.find(filters=[Filter("rank", "in", [1, 2]), predicates[0]], limit=1000, cursor=page.next_cursor)
        self.assertEqual([row.key for row in page.rows + next_page.rows], ["a", "c"])

    def test_actual_filter_query_uses_declared_index(self):
        self.populate()
        statements = []
        connection = self.store._connection()
        connection.set_trace_callback(statements.append)
        self.items.find(filters=[Filter("done", "eq", True)], order=[Order("rank")])
        connection.set_trace_callback(None)
        query = next(sql for sql in statements if sql.startswith('SELECT * FROM "app_work_items"'))
        plan = connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        self.assertTrue(any("app_items_done_rank_key" in row[3] for row in plan), plan)

    def test_concurrent_change_is_a_fresh_page_not_a_snapshot(self):
        self.populate()
        first = self.items.find(limit=2)
        self.items.create(WorkItem("aa", "Before the cursor"))
        self.items.create(WorkItem("bb", "After the cursor"))
        self.items.delete("c", expected_revision="1")
        rest = self.items.find(limit=100, cursor=first.next_cursor)
        self.assertEqual([row.key for row in rest.rows], ["bb", "d", "e", "f"])

    def test_cursor_output_bound_is_enforced_for_long_sort_values(self):
        self.items.create(WorkItem("a", "x" * 20_000))
        self.items.create(WorkItem("b", "z" * 20_000))
        with self.assertRaises(ValidationError):
            self.items.find(order=[Order("title")], limit=1)
