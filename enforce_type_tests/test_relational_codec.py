from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backpack_store import ApplicationStore, ValidationError
from backpack_store.enforce_type_codec import EnforceTypeCodec


@dataclass
class Item:
    key: str
    title: str = "Garden"
    done: bool = False
    rank: int | None = None


class EnforceRelationalTests(unittest.TestCase):
    def test_preferred_codec_defaults_checked_writes_and_reopen(self):
        codec = EnforceTypeCodec(Item, type_key="garden.item")
        options = dict(namespace="garden", table="app_items", primary_key="key",
                       columns={name: name for name in Item.__dataclass_fields__})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "application.sqlite"
            with ApplicationStore.open(path) as store:
                items = store.register(codec, **options)
                store.initialize("garden")
                value = codec.decode({"key": "a"})
                row = items.create(value)
                with self.assertRaises(ValidationError):
                    items.replace("a", Item("a", done=1), expected_revision="1")
                self.assertEqual(items.get("a"), row)
                changed = items.replace("a", Item("a", "Updated", True, 3), expected_revision="1")
            with ApplicationStore.open(path) as store:
                items = store.register(codec, **options)
                store.initialize("garden")
                self.assertEqual(items.get("a"), changed)
