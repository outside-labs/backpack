from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from backpack_store import (
    AlreadyExistsError, ApplicationStore, Backpack, ConstraintError, DataclassCodec,
    DatabaseVersionError, ForeignKey, Index, NotFoundError, RegistrationError,
    RevisionConflictError, StorageError, StoreClosedError, ValidationError,
)


@dataclass
class Project:
    key: str
    name: str


@dataclass
class WorkItem:
    key: str
    title: str
    done: bool = False
    project_key: str | None = None
    rank: int | None = None
    estimate: float | None = None


def register(store, codec_type=DataclassCodec, *, project_indexes=()):
    projects = store.register(codec_type(Project, type_key="garden.project"), namespace="garden",
                              table="app_projects", primary_key="key", columns={"key": "key", "name": "name"},
                              unique=(("name",),), indexes=project_indexes)
    items = store.register(codec_type(WorkItem, type_key="garden.item"), namespace="garden",
                           table="app_work_items", primary_key="key",
                           columns={name: name for name in WorkItem.__dataclass_fields__},
                           foreign_keys=(ForeignKey("project_key", "garden.project"),),
                           indexes=(Index("app_items_done_rank_key", ("done", "rank", "key")),))
    return projects, items


class ApplicationFixture:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "application.sqlite"
        self.store = ApplicationStore.open(self.path)
        self.addCleanup(self.store.close)
        self.projects, self.items = register(self.store)
        self.store.initialize("garden")


class RelationalTests(ApplicationFixture, unittest.TestCase):
    def test_create_reopen_detachment_and_legacy_records(self):
        value = WorkItem("item-01", "Garden", estimate=1.5)
        row = self.items.create(value)
        self.assertEqual((row.key, row.revision), (value.key, "1"))
        self.assertEqual(str(UUID(row.storage_uuid)), row.storage_uuid)
        self.assertNotEqual(row.key, row.storage_uuid)
        value.title = "local edit"
        row.value.title = "another local edit"
        self.assertEqual(self.items.get(row.key).value.title, "Garden")
        with Backpack.open(self.path) as legacy:
            legacy.register(DataclassCodec(Project, type_key="legacy.project"))
            identity = legacy.put(Project("old", "Existing JSON")).id
        self.store.close()
        with ApplicationStore.open(self.path) as reopened:
            _, items = register(reopened)
            reopened.initialize("garden")
            actual = items.get(row.key)
            self.assertEqual(actual.storage_uuid, row.storage_uuid)
            self.assertEqual(actual.value, WorkItem("item-01", "Garden", estimate=1.5))
        with Backpack.open(self.path) as legacy:
            legacy.register(DataclassCodec(Project, type_key="legacy.project"))
            self.assertEqual(legacy.get(identity), Project("old", "Existing JSON"))

    def test_unique_foreign_keys_and_failed_reservation(self):
        self.projects.create(Project("garden", "Garden"))
        with self.assertRaises(AlreadyExistsError):
            self.projects.create(Project("other", "Garden"))
        self.projects.create(Project("other", "Other"))
        with self.assertRaises(ConstraintError):
            self.items.create(WorkItem("retry", "Garden", project_key="absent"))
        row = self.items.create(WorkItem("retry", "Garden", project_key="garden"))
        with self.assertRaises(ConstraintError):
            self.projects.delete("garden", expected_revision="1")
        self.items.delete(row.key, expected_revision="1")
        self.projects.delete("garden", expected_revision="1")
        with self.assertRaises(AlreadyExistsError):
            self.items.create(WorkItem("retry", "Key remains reserved"))

    def test_checked_replacement_missing_precedence_and_immutable_keys(self):
        created = self.items.create(WorkItem("a", "One"))
        changed = self.items.replace("a", WorkItem("a", "Two", True), expected_revision="1")
        self.assertEqual(changed.revision, "2")
        self.assertEqual(changed.storage_uuid, created.storage_uuid)
        for operation in (lambda: self.items.replace("a", WorkItem("a", "Stale"), expected_revision="1"),
                          lambda: self.items.delete("a", expected_revision="1")):
            with self.assertRaises(RevisionConflictError):
                operation()
        with self.assertRaises(ValidationError):
            self.items.replace("a", WorkItem("b", "Renamed"), expected_revision="2")
        with self.assertRaises(NotFoundError):
            self.items.delete("missing", expected_revision="9")
        self.assertEqual(self.items.get("a"), changed)

    def test_native_types_json_bounds_and_revision_overflow(self):
        invalid = [WorkItem("a", "x", done=1), WorkItem("a", "x", rank=True),
                   WorkItem("a", "x", rank=2**53), WorkItem("a", "x", estimate=1),
                   WorkItem("a", "x", estimate=float("inf")), WorkItem("bad key", "x"),
                   WorkItem("a", "\ud800")]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.items.create(value)
        self.items.create(WorkItem("a", "Valid", estimate=0.0))
        for revision in (1, True, "01", "0", "-1", "١", str(2**63)):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                self.items.delete("a", expected_revision=revision)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE app_work_items SET _revision=?", (2**63-1,))
        with self.assertRaises(RevisionConflictError):
            self.items.replace("a", WorkItem("a", "Overflow"), expected_revision=str(2**63-1))
        self.assertEqual(self.items.get("a").value.title, "Valid")

    def test_bounded_listing_uses_domain_key_order_and_validates_limits(self):
        for key in ("c", "a", "b"):
            self.items.create(WorkItem(key, "Garden"))
        self.assertEqual([row.key for row in self.items.list(limit=2)], ["a", "b"])
        self.assertEqual([row.key for row in self.items.list(limit=2, after="b")], ["c"])
        for limit in (True, 0, 1001, "1"):
            with self.assertRaises(ValidationError):
                self.items.list(limit=limit)

    def test_schema_tampering_and_unknown_versions_are_refused_without_mutation(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE app_work_items ADD COLUMN unexpected TEXT")
        with ApplicationStore.open(self.path) as reopened:
            register(reopened)
            with self.assertRaises(DatabaseVersionError):
                reopened.initialize("garden")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE bp_application_namespaces SET schema_version=99")
        with ApplicationStore.open(self.path) as reopened:
            register(reopened)
            with self.assertRaises(DatabaseVersionError):
                reopened.initialize("garden")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM bp_application_namespaces").fetchone()[0], 99)

    def test_unowned_tables_are_not_adopted_and_metadata_rolls_back(self):
        path = Path(self.directory.name) / "unowned.sqlite"
        with Backpack.open(path):
            pass
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE app_projects (key TEXT)")
        with ApplicationStore.open(path) as store:
            register(store)
            with self.assertRaises(DatabaseVersionError):
                store.initialize("garden")
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='bp_application_namespaces'").fetchone())

    def test_registration_is_explicit_atomic_and_has_no_schema_io(self):
        with ApplicationStore.open(":memory:") as store:
            codec = DataclassCodec(Project, type_key="garden.project")
            options = dict(namespace="garden", table="app_projects", primary_key="key", columns={"key": "key", "name": "name"})
            for changes in ({"table": "records"}, {"table": "app_x;DROP TABLE records"},
                            {"columns": {"key": "same", "name": "same"}}, {"primary_key": "missing"},
                            {"indexes": (Index("app_bad", ("missing",)),)}):
                with self.assertRaises(RegistrationError):
                    store.register(codec, **(options | changes))
            model = store.register(codec, **options)
            with self.assertRaises(RegistrationError):
                model.get("a")
            self.assertEqual(store._connection().execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'app_%'").fetchone()[0], 0)
            store.initialize("garden")
            model.create(Project("a", "Garden"))
            with self.assertRaises(RegistrationError):
                store.register(codec, **options)

    def test_unsupported_annotations_and_missing_foreign_target(self):
        @dataclass
        class Nested:
            key: str
            values: list[str]
        with ApplicationStore.open(":memory:") as store:
            with self.assertRaises(RegistrationError):
                store.register(DataclassCodec(Nested, type_key="test.nested"), namespace="garden", table="app_nested",
                               primary_key="key", columns={"key": "key", "values": "values"})
            store.register(DataclassCodec(WorkItem, type_key="garden.item"), namespace="garden", table="app_items",
                           primary_key="key", columns={name: name for name in WorkItem.__dataclass_fields__},
                           foreign_keys=(ForeignKey("project_key", "missing.parent"),))
            with self.assertRaises(RegistrationError):
                store.initialize("garden")

    def test_corrupt_rows_are_value_free_and_thread_ownership_is_explicit(self):
        self.items.create(WorkItem("a", "private submitted value"))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute("UPDATE app_work_items SET done=9")
        with self.assertRaises(StorageError) as caught:
            self.items.get("a")
        self.assertNotIn("private submitted value", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaises(StorageError):
                executor.submit(self.items.list).result()
        self.store.close()
        with self.assertRaises(StoreClosedError):
            self.items.list()
