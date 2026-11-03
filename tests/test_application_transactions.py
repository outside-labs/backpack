from dataclasses import replace
import unittest

from backpack_store import (
    AlreadyExistsError, ApplicationStore, NotFoundError, RevisionConflictError,
    TransactionError, ValidationError,
)
from test_relational import ApplicationFixture, Project, WorkItem, register


class ApplicationTransactionTests(ApplicationFixture, unittest.TestCase):
    def test_nested_initialization_preserves_handle_readiness(self):
        with self.store.unit_of_work():
            with self.assertRaises(TransactionError):
                self.store.initialize("garden")
            self.items.create(WorkItem("a", "Still initialized"))
        self.assertEqual(self.items.get("a").revision, "1")

    def test_multi_operation_commit_and_detached_read_your_writes(self):
        with self.store.unit_of_work():
            self.projects.create(Project("garden", "Garden"))
            row = self.items.create(WorkItem("a", "One", project_key="garden"))
            result = self.items.update("a", expected_revision=row.revision,
                                       transform=lambda value: replace(value, title="Two"))
            self.assertEqual(self.items.get("a"), result)
        self.assertEqual(self.items.get("a").revision, "2")

    def test_two_operation_failure_rolls_back_rows_and_reservations(self):
        with self.assertRaises(AlreadyExistsError):
            with self.store.unit_of_work():
                self.projects.create(Project("garden", "Garden"))
                self.projects.create(Project("other", "Garden"))
        self.assertEqual(self.projects.list(), ())
        self.projects.create(Project("garden", "Retry"))

    def test_caught_database_failure_marks_whole_unit_for_rollback(self):
        with self.assertRaises(TransactionError):
            with self.store.unit_of_work():
                self.projects.create(Project("garden", "Garden"))
                try:
                    self.projects.create(Project("other", "Garden"))
                except AlreadyExistsError:
                    pass
        self.assertEqual(self.projects.list(), ())

    def test_failed_transform_validation_and_key_change_preserve_prior_rows(self):
        original = self.items.create(WorkItem("a", "One"))
        def failing(value):
            value.title = "unsaved mutation"
            raise ValueError("private callback value")
        for transform, error in ((failing, TransactionError),
                                 (lambda value: replace(value, done=1), ValidationError),
                                 (lambda value: replace(value, key="b"), ValidationError)):
            with self.subTest(transform=transform), self.assertRaises(error) as caught:
                self.items.update("a", expected_revision="1", transform=transform)
            self.assertNotIn("private callback value", str(caught.exception))
            self.assertEqual(self.items.get("a"), original)
        with self.assertRaises(ValidationError):
            with self.store.unit_of_work():
                self.projects.create(Project("garden", "Garden"))
                self.items.update("a", expected_revision="1", transform=lambda value: replace(value, done=1))
        self.assertEqual(self.projects.list(), ())

    def test_competing_checked_updates_and_missing_result(self):
        self.items.create(WorkItem("a", "One"))
        with ApplicationStore.open(self.path) as other:
            _, items = register(other)
            other.initialize("garden")
            first = self.items.update("a", expected_revision="1", transform=lambda value: replace(value, title="Winner"))
            called = []
            with self.assertRaises(RevisionConflictError):
                items.update("a", expected_revision="1", transform=lambda value: called.append(value))
            self.assertEqual(called, [])
            self.assertEqual(items.get("a"), first)
            with self.assertRaises(NotFoundError):
                items.update("missing", expected_revision="1", transform=lambda value: value)

    def test_nested_units_and_recursive_transforms_are_refused(self):
        self.items.create(WorkItem("a", "One"))
        with self.store.unit_of_work():
            with self.assertRaises(TransactionError):
                with self.store.unit_of_work():
                    self.fail("nested unit must not be entered")
            with self.assertRaises(TransactionError):
                self.store.close()
        with self.assertRaises(TransactionError):
            self.items.update("a", expected_revision="1", transform=lambda value: self.items.get("a").value)
        self.assertEqual(self.items.get("a").revision, "1")

    def test_async_transforms_are_refused_without_execution(self):
        self.items.create(WorkItem("a", "One"))
        async def transform(value):
            return value
        with self.assertRaises(ValidationError):
            self.items.update("a", expected_revision="1", transform=transform)
        with self.assertRaises(TransactionError):
            self.items.update("a", expected_revision="1", transform=lambda value: transform(value))
        self.assertEqual(self.items.get("a").revision, "1")
