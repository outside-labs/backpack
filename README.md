# Backpack

An experimental local library for typed records, identity, and provenance. Its
Python package is `backpack_store`; its distribution is `outside-labs-backpack`.
The package identity avoids the unrelated existing `backpack` distribution.

Python 3.11 or newer is the selected baseline, matching the capability family.
The library has no mandatory runtime dependencies. This first checkpoint removes
the generated greeting executable; codecs and durable storage follow in separate
changes. The original scaffold remains recoverable in the initial commit.

Persistence belongs to this library. Capability registration, protocol adapters,
remote accounts, and implicit synchronization are separate concerns. Importing
this package does not open a file or database.

## Local build

```sh
uv build
```

Package upload and remote setup are separate actions. The 0.1.x API remains
experimental.
