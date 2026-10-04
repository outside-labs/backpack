"""Optional selected tools around an explicitly configured local store."""

from dataclasses import dataclass
from typing import Any

from hidden_moves import MoveAnnotations, MoveSpec

from .errors import BackpackError, CodecError
from .records import Provenance, Record, json_object
from .store import Backpack, WriteResult

READ_CAPABILITIES = ("backpack.records.get", "backpack.records.find")
WRITE_CAPABILITIES = ("backpack.records.put", "backpack.records.delete")


@dataclass(frozen=True)
class RecordView:
    id: str
    type_key: str
    schema_version: int
    revision: int
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    tags: list[str]
    provenance: Provenance | None


@dataclass(frozen=True)
class RecordPage:
    records: list[RecordView]
    next_cursor: str | None


def _view(record: Record) -> RecordView:
    return RecordView(
        record.id,
        record.type_key,
        record.schema_version,
        record.revision,
        record.payload,
        record.created_at,
        record.updated_at,
        list(record.tags),
        record.provenance,
    )


def get_record(store: Backpack, record_id: str) -> RecordView:
    """Read a local record through its explicitly registered codec."""
    return _view(store.get_record(record_id))


def find_records(
    store: Backpack,
    type_key: str,
    tags: list[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> RecordPage:
    """Read a bounded page for one registered type and all requested exact tags."""
    codec = store.codec_for_key(type_key)
    page = store.find(
        codec.model_type, tags=() if tags is None else tags, limit=limit, cursor=cursor
    )
    return RecordPage([_view(record) for record in page.records], page.next_cursor)


def put_record(
    store: Backpack,
    type_key: str,
    payload: dict[str, Any],
    tags: list[str] | None = None,
    provenance: Provenance | None = None,
) -> WriteResult:
    """Insert a new local UUID after validating the current registered payload type."""
    codec = store.codec_for_key(type_key)
    try:
        value = codec.decode(json_object(payload))
    except BackpackError:
        raise
    except Exception:  # noqa: BLE001 - trusted custom codec failures must not expose payloads
        raise CodecError(
            "registered codec failed to decode the supplied payload"
        ) from None
    if type(value) is not codec.model_type:
        raise CodecError("registered codec returned a different Python type")
    return store.put(value, tags=() if tags is None else tags, provenance=provenance)


def delete_record(store: Backpack, record_id: str) -> bool:
    """Delete one explicit local UUID, reporting whether it existed."""
    return store.delete(record_id)


def provide_moves() -> tuple[MoveSpec, ...]:
    """Advertise definitions without opening a database or choosing codecs."""
    read = MoveAnnotations(
        read_only=True, destructive=False, idempotent=True, external=False
    )
    insert = MoveAnnotations(
        read_only=False, destructive=False, idempotent=False, external=False
    )
    delete = MoveAnnotations(
        read_only=False, destructive=True, idempotent=True, external=False
    )
    return tuple(
        MoveSpec(
            name=name,
            namespace="backpack.records",
            func=operation,
            bind_target=True,
            target_types=(Backpack,),
            annotations=annotations,
        )
        for name, operation, annotations in (
            ("get", get_record, read),
            ("find", find_records, read),
            ("put", put_record, insert),
            ("delete", delete_record, delete),
        )
    )
