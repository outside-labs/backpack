# Backpack

Backpack is an experimental programmable local store for typed records. The
current foundation defines explicit codecs, record identity and provenance; the
SQLite persistence API is the next milestone.

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
