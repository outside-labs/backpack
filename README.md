# Backpack

Backpack is an experimental programmable local store for typed records. The
SQLite API stores finite JSON with explicit codecs, local identity and provenance.
Records recover their Python types after closing and reopening the database.

- Product: Backpack
- Distribution: `outside-labs-backpack`
- Import: `backpack_store`
- Python: 3.11 or newer
- Runtime dependencies: none

## Explicit typed records

```python
from dataclasses import dataclass
from backpack_store import CodecRegistry, DataclassCodec

@dataclass(frozen=True)
class Note:
    title: str
    body: str

registry = CodecRegistry()
registry.register(DataclassCodec(Note, type_key="notes.note", schema_version=1))
encoded = registry.encode(Note("Example", "Local text"))
restored = registry.decode(encoded.type_key, encoded.schema_version, encoded.payload)
assert isinstance(restored, Note)
```

A stable dotted `type_key` and positive `schema_version` identify each registered
codec. Registrations are explicit and reject duplicate keys or Python types.
Dataclass codecs support concrete non-recursive dataclasses, nested dataclasses,
JSON scalar fields, literals, optional/unions, typed lists, tuples and string-keyed
dictionaries. They validate every field; unknown or missing fields fail. A custom
codec can support other trusted model types. Stored names never cause imports.

Record envelopes contain a separate local UUID, payload schema version, revision,
UTC creation/update timestamps, normalized exact tags and optional `Provenance`.
Timestamps use `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Provenance names the source URL,
system, object ID and capture time; a source ID is not the local record UUID.
JSON payloads must be finite objects, at most 1 MiB and 64 nesting levels. Tags
are case-sensitive NFC strings, trimmed, deduplicated and sorted (up to 64 supplied
values of 128 characters). Unknown types/versions and invalid payloads raise typed
errors. Importing the package opens no files or database and performs no network I/O.

## Durable local storage

```python
from backpack_store import Backpack

with Backpack.open("notes.sqlite") as store:
    store.register(DataclassCodec(Note, type_key="notes.note"))
    written = store.put(Note("Example", "Local text"), tags=["work"])

with Backpack.open("notes.sqlite") as store:
    store.register(DataclassCodec(Note, type_key="notes.note"))
    note = store.get(written.id)
    record = store.get_record(written.id)  # envelope plus .value
    page = store.find(Note, tags=["work"], limit=20)
    updated = store.update(written.id, Note("Revised", "Local text"),
                           expected_revision=record.revision, tags=["work"])
    assert updated.revision == 2
    assert store.delete(written.id)
```

Each `put` creates a new UUID and returns `WriteResult(id, revision)`. `update`
replaces payload, tags and provenance together, preserves the local ID/type key
and creation time, and requires the expected revision. A stale revision raises
`RevisionConflictError` without changing the record. Missing reads/updates raise
`NotFoundError`; deletion returns whether a row existed. Payload, metadata and
tag writes share one transaction. Invalid input and database failures leave no
partial record.

`find` requires an explicitly registered Python type and matches **all** requested
tags. It returns at most 1,000 records (default 50) in ascending local UUID order.
Pass `.next_cursor` with the same type/tags to continue. Cursors are query-scoped;
concurrent inserts, updates or deletions can change later pages, so pagination
does not promise a frozen snapshot. `get_raw` returns recoverable JSON/metadata
without decoding an unknown registered type or payload version.

The database schema has its own version, separate from payload versions. Unknown
or nonempty unversioned databases fail safely. Use the connection on its opening
thread; a context manager closes it even after caller failure. Lock waiting is
bounded by `timeout` (default 5 seconds, allowed 0–60). No path is chosen by
default. Parent directories must already exist. Importing never opens a database.

Run `python examples/persistence.py` for a complete offline file-backed Note and
synthetic ProjectItem round trip, including filtering and deletion.

## Development

```sh
uv build
python -m pip install dist/outside_labs_backpack-0.1.0-py3-none-any.whl
python -m unittest discover -s tests -v
```

CI installs the wheel built from its source distribution and runs the behavior
suite on Python 3.11/3.13 across Linux, macOS and Windows. The original generated
scaffold is preserved in the repository's initial commit.

This 0.1.x project is experimental. No package publication or hosted service is
part of this milestone.
