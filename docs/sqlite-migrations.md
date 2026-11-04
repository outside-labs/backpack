# SQLite snapshots and migrations

These standard-library facilities implement reusable mechanics from
[Pretty Notebook's inventory](https://github.com/wispsy/pretty-notebook/blob/main/docs/storage-extraction.md).
Consumer schemas, ORM connections, authentication, filesystem publication and
whole-site recovery remain application-owned.

## Private snapshots

```python
from backpack_store import snapshot_sqlite, verify_snapshot

receipt = snapshot_sqlite("application.sqlite", "migration-backups")
verify_snapshot(receipt)
```

SQLite's backup API includes committed WAL data and excludes uncommitted writes.
Source and destination connections are separately owned and closed. A new
unpredictable file is created without overwriting existing files. Symlink
destinations/sources and nonregular sources are refused. New POSIX directories
and files use 0700/0600; existing directories must be private. Windows directory
ACL ownership remains the caller's responsibility.

Integrity checks, file/directory synchronization (directory sync on POSIX) and
SHA-256 hashing finish before returning `SnapshotReceipt(path, sha256,
size_bytes)`. Failure removes the incomplete file. Backup progress has a
positive configurable time bound up to 300 seconds (default 30). Prefixes use
bounded lowercase ASCII. Async consumers keep this entire synchronous operation
on one selected worker and reconcile cancellation after dispatch.

A receipt verifies the database copy, not complete application recovery.
Retain matching code/schema, blobs, settings and other consumer state.

## Ordered upgrades

```python
from backpack_store import MigrationStep, migrate_sqlite

def initial(connection):
    connection.execute("CREATE TABLE notes (key TEXT PRIMARY KEY, title TEXT)")

def add_rank(connection):
    connection.execute("ALTER TABLE notes ADD COLUMN rank INTEGER")

result = migrate_sqlite(
    "notes.sqlite", namespace="notes", backup_directory="migration-backups",
    steps=[MigrationStep(1, "notes", initial),
           MigrationStep(2, "note-ranks", add_rank)],
)
```

Provide the complete contiguous version/name plan at every startup. The runner
enables foreign keys and acquires `BEGIN IMMEDIATE` before reading history or
making a snapshot, serializing independent processes through SQLite's writer
lock. Lock waiting is bounded to 0–60 seconds (default five). Fresh/current
schemas avoid unnecessary backups. An existing database upgrade requires a
verified backup before schema/history mutation.

All pending steps and history rows commit together. Unknown/newer versions,
gaps, changed names, missing owned history and malformed receipts stop before
mutation. Rows keep exact names, UTC times and backup path/hash receipts.
Backup failure prevents upgrades; migration failure rolls back schema/history
and retains the usable snapshot. A synchronous `validate(connection, version)`
callback checks current state and each step; final foreign-key checks must pass.
Callbacks cannot commit, rollback, use savepoints, attach databases or escape
the transaction with `executescript`. Use `execute`/`executemany` for individual
statements and keep external effects outside callbacks.

Namespaces share scoped `bp_schema_migrations` history with separate ownership
records by default. Select consumer history explicitly, for example
`MigrationHistory("pnbp_schema_migrations", scoped=False)` and
`adopt_history=True` on first adoption. Existing names/times/receipts remain
unchanged. An unscoped table cannot serve multiple namespaces. The library does
not discover, rename or rewrite consumer history. Foreign keys stay enabled;
consumers needing a pre-BEGIN table rebuild retain their existing migration
owner until that procedure is separately adapted.

## Registered application upgrades

`ApplicationStore.upgrade(namespace, expected_version=..., migrations=...,
backup_directory=..., validate_current=...)` integrates the runner with owned
models. Register the complete target models; provide every upgrade step from
version 2 and a callback validating the current schema/data at the expected
version. The version-1 `initial-schema` baseline is recorded, the exact target
DDL is validated and the namespace signature/version updates atomically.
`model.schema_sql` exposes canonical target DDL for authored migrations.

Reopen with `initialize(namespace, schema_version=target,
migrations=complete_upgrade_steps)` to validate history/DDL without running
callbacks or backing up again. Fresh schemas start at version 1. Migration SQL
must preserve keys, UUID reservations and revisions. Older code requires the
matching restored pre-upgrade database. `PRAGMA user_version` and JSON payload
evolution stay independent.
