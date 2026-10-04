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

## Version evolution and export

Register the current codec, then explicit trusted functions for every one-version
step from an older payload to the current version:

```python
store.register(DataclassCodec(NoteV2, type_key="notes.note", schema_version=2))
store.register_migration("notes.note", from_version=1, to_version=2,
                         migrate=lambda old: {"title": old["title"], "text": old["body"]})
```

`NoteV2` is an application-defined dataclass with `title` and `text` fields. Old
reads upgrade in memory; `.payload` and `.schema_version` on the returned envelope
still describe the original stored representation. Only an explicit
revision-checked `update` writes the current representation. Missing migration
steps and newer unknown versions raise `UnknownVersionError`; failed/invalid
migration output raises `MigrationError`. Migration functions never come from a
stored module name. Database schema version 1 is independent; no destructive
migration or automatic database replacement is provided.

```python
page = store.export_jsonl("records-1.jsonl", limit=100, max_bytes=4_194_304)
# If page.next_cursor is set, export to another new explicit destination:
# store.export_jsonl("records-2.jsonl", cursor=page.next_cursor, limit=100)
```

Export writes UTF-8 JSONL containing raw record envelopes with `format_version: 1`,
type/payload versions and provenance. No codec or migration runs during export.
It stops at the record or byte budget (default 1,000 records / 4 MiB; maximum
1,000 / 64 MiB). If a single record exceeds the byte budget, it fails before
creating a destination. Pages use ascending UUIDs and the same concurrent-write
limitations as `find`. A complete temporary file is published without overwriting
an existing path; the destination directory must exist and support hard links.
Export never rewrites the database.

## Optional selected tools

The direct store remains independent of Hidden Moves and MCP. Optional `moves`
and `host` extras declare the capability-family dependencies; those distributions
are currently installed from their source-built wheels. CI uses a reviewed pinned
family revision, runs standalone storage first, then installs the optional wheels.

Installed entry-point discovery advertises `backpack`; load it explicitly and bind
an already-configured Backpack instance. The factory chooses no path and opens no
database. The application registers trusted models before selecting tools:

```python
from backpack_store.host import build_catalog
from backpack_store.integration import READ_CAPABILITIES, WRITE_CAPABILITIES

read = build_catalog(store, READ_CAPABILITIES)
result = read.invoke("backpack.records.find", {"type_key": "notes.note", "limit": 10})
write = build_catalog(store, WRITE_CAPABILITIES, allow_writes=True)
```

Read tools are `backpack.records.get` and `.find`; local write tools are `.put` and
`.delete`. A write profile requires explicit host intent and explicit selection.
Inputs contain local IDs, registered stable type keys and finite JSON payloads,
never Python classes, codec imports, SQL or database paths. `put` validates the
current registered model; `find` enforces the storage query limits. Concrete JSON
views preserve record identity/version/provenance without serializing arbitrary
Python objects. Effect annotations describe local reads/insertion/deletion;
application permissions and caller approval policy remain responsible for access.

`serve_store(store, names, allow_writes=False)` serves the same catalog over local
MCP stdio. The application owns the connection and cleanup. For a complete notes
application with a fixed trusted model and explicit path/profile:

```sh
python examples/notes_stdio.py --database notes.sqlite --profile read
# Select a write-capable host only when local writes are intended:
python examples/notes_stdio.py --database notes.sqlite --profile write
```

These are local tools. No public endpoint or remote access to the SQLite file is
provided. Run `python -m unittest discover -s integration_tests -v` in the optional
integration environment to check real stdio, selection and consumer equivalence.

## Optional Pydantic model codec

Install the `pydantic` extra to use the separate Pydantic 2 extension (supported
range `>=2.13,<3`; tested with 2.13.5). Importing the core never imports Pydantic.
The extension uses the same store, explicit identity and migration registry:

```python
from pydantic import BaseModel
from backpack_store.pydantic_codec import PydanticCodec

class ValidatedNote(BaseModel):
    title: str
    priority: int

store.register(PydanticCodec(ValidatedNote, type_key="notes.validated", schema_version=1))
written = store.put(ValidatedNote(title="Example", priority=2))
```

Register the same trusted model after reopening. The codec accepts the exact
registered class, serializes aliases and JSON representations, then revalidates
strictly before writing. Decode also uses strict JSON validation with extra fields
forbidden. This catches mutated instances and `model_construct` bypasses instead
of trusting an already-created instance. Payloads still obey the core finite JSON
object limits. Object-shaped nested models and datetime JSON round trips are
covered; scalar/list `RootModel` classes are rejected. Private/computed fields are
not part of the durable representation.

This codec does not add native Pydantic type support to tool signatures. The
optional provider continues to use its concrete JSON views. Run
`python -m unittest discover -s pydantic_tests -v` in the extension environment.

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
