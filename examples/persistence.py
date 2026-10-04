"""Offline, on-disk round trip for a typed note and a synthetic Project item."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from backpack_store import Backpack, DataclassCodec, Provenance
from backpack_store.records import utc_now


@dataclass(frozen=True)
class Note:
    title: str
    body: str


@dataclass(frozen=True)
class ProjectItem:
    project_id: str
    item_id: str
    content_id: str | None
    status: str


def register(store: Backpack) -> None:
    store.register(DataclassCodec(Note, type_key="notes.note"))
    store.register(DataclassCodec(ProjectItem, type_key="github.project_item"))


def main() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "records.sqlite"
        note = Note("Example", "Durable local text")
        item = ProjectItem("PVT_fixture", "PVTI_fixture", "I_fixture", "Todo")
        provenance = Provenance(
            source_system="github-fixture",
            source_id=item.item_id,
            captured_at=utc_now(),
        )
        with Backpack.open(path) as store:
            register(store)
            note_write = store.put(note, tags=["notes"])
            item_write = store.put(item, tags=["snapshot"], provenance=provenance)
        with Backpack.open(path) as store:
            register(store)
            assert store.get(note_write.id) == note
            record = store.get_record(item_write.id)
            assert record.value == item and record.provenance == provenance
            assert record.id != item.item_id and record.schema_version == 1
            assert store.find(ProjectItem, tags=["snapshot"], limit=1).records == (
                record,
            )
            assert store.delete(note_write.id)
            assert store.find(Note).records == ()
    print(
        "Reopened typed note and Project item with identity/provenance; filtered and deleted locally."
    )


if __name__ == "__main__":
    main()
