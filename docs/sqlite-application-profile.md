# One-host SQLite application profile

`ApplicationStore.open(path, timeout=5.0, journal_mode="wal")` selects the
application profile explicitly. Its async facade accepts the same settings.
Each independent process opens its own store; synchronous connections remain
on their opening thread, and an async facade keeps its connection on one owned
worker. Never pass connections or facades through a process fork.

## Database and lifecycle

- File-backed applications default to WAL, with `synchronous=FULL` and foreign
  keys enabled on each owned connection. Explicit `journal_mode="delete"`
  selects rollback journaling. In-memory stores use SQLite's ephemeral memory
  journal and do not qualify durable deployment.
- SQLite serializes writers. `BEGIN IMMEDIATE` owns writes and migration
  startup; lock waiting is bounded to the selected 0–60 second timeout. Initial
  journal selection also retries transient busy/locked results within that
  bound. More workers do not add simultaneous SQLite writers.
- Default reads return currently committed detached rows. There is no
  process-local model cache. Every checked write reads the revision under its
  write transaction; missing rows and stale revisions have separate results.
- A unit of work commits all enlisted operations or rolls them back, including
  new key reservations. Process exit before commit leaves the prior committed
  state recoverable. It cannot undo external callback effects.
- Async admission and close use the [owned facade](application-storage.md).
  Queued cancellation skips work; dispatched cancellation retains a result
  receipt. Closing drains accepted work before thread/connection cleanup.
- Run schema initialization/upgrades before accepting requests. Use one
  application/schema version and one journal profile across all workers.
  Stop workers before changing journal profiles or restoring a database.

The [migration runner](sqlite-migrations.md) acquires the database writer lock
before checking history and taking its pre-upgrade snapshot. Concurrent
starters therefore apply each step once and retain one applicable backup.
An upgrade failure rolls back history/schema and retains the prior snapshot.
Unknown/malformed history stops startup. Consumer history adoption is explicit.

## Filesystem and recovery limits

This profile covers a single host with a local filesystem supporting SQLite
locking, shared memory and synchronization. It does not qualify network-mounted
or multi-host storage. Protect the database directory, WAL/SHM files and backup
directory with application-owned filesystem permissions/ACLs. Keep worker
connections independent and use a shared actual database path.

Durability depends on the operating system, filesystem and hardware honoring
SQLite's synchronization requests. Process-exit recovery is exercised; sudden
power loss and storage hardware faults are outside this acceptance. Use the
backup API to include committed WAL state rather than copying a live main
database file alone. Verify receipts and preserve matching schema/code and all
consumer files before recovery. No automatic restore or old-code downgrade is
performed.

## Executable qualification

Run `python -m unittest discover -s process_tests -v` against an installed
distribution. Six cases use the standard-library spawn process context:

1. Four simultaneous starters, one create winner, one checked-update winner,
   visibility through every worker, exact PRAGMA settings and restart.
2. Four fresh migration starters, with each history step applied once.
3. Four upgrade starters, with one verified backup and preserved data.
4. Abrupt exit inside a two-operation write, followed by row/revision and key
   reservation recovery.
5. An independent writer refusing a held lock within its selected timeout.
6. A cancelled caller in another process, outcome receipt reconciliation,
   independent-reader visibility and worker shutdown.

CI runs these cases on Linux (Python 3.11/3.13), macOS (3.13) and Windows (3.11).
Linux Python 3.14 also selects published Enforce Type 0.1.0 through
`BACKPACK_PROCESS_CODEC=enforce-type`. Base tests separately cover the explicit
rollback-journal mode. This is bounded acceptance for the stated profile,
not a throughput or worker-count capacity guarantee.
