# Relational application storage

`ApplicationStore` implements the scalar model and checked CRUD portion of
[`relational-sqlite/1`](relational-contract-v1.md). It coexists with the existing
`Backpack` JSON-record API in the same database, with separate application
schema metadata, domain keys and string revisions. Both connections stay on
their opening thread and close explicitly.

```python
from dataclasses import dataclass
from backpack_store import ApplicationStore
from backpack_store.enforce_type_codec import EnforceTypeCodec

@dataclass
class Item:
    key: str
    title: str
    done: bool = False

with ApplicationStore.open("application.sqlite") as store:
    items = store.register(
        EnforceTypeCodec(Item, type_key="garden.item"),
        namespace="garden", table="app_items", primary_key="key",
        columns={"key": "key", "title": "title", "done": "done"},
    )
    store.initialize("garden")
    created = items.create(Item("item-001", "Garden"))
    edited = items.replace(created.key, Item(created.key, "Garden", True),
                           expected_revision=created.revision)
    assert edited.revision == "2"
    assert items.list(limit=10)[0].value.done
```

This preferred codec requires Python 3.14+ and the `[enforce-type]` extra.
Explicit compatible codecs such as `DataclassCodec` keep the base Python 3.11+
profile available. Native values must have exact scalar types, with finite
floats and portable JSON integers. Nullable columns derive from explicit
optional annotations; defaults are applied by trusted model construction.

Registration performs no schema I/O. Register the complete namespace before
`initialize`; repeated initialization validates the exact owned schema. Unknown
versions, unowned tables and incompatible registrations are refused. Indexes
and unique groups name mapped fields; `ForeignKey(field, target_type_key)`
references another registered string primary key in the same namespace.

Rows contain a detached `value`, immutable `key`, opaque string `revision` and
internal `storage_uuid`. Checked replace/delete require the expected revision.
Missing rows raise `NotFoundError`; stale writes raise `RevisionConflictError`.
Unique or permanently reserved keys raise `AlreadyExistsError`; invalid
references raise `ConstraintError`. Failed writes preserve the previous row
and release any uncommitted reservation. Successful deletion retains the key
reservation permanently, preventing stale clients from modifying a recreated
resource. Errors omit submitted values and SQL.

`list(limit=1..1000, after=key)` provides bounded primary-key traversal. Typed
filters and query-bound cursors are owned by BP-QUERY-01; upgrades remain in
their subsequent task.

## Atomic updates and units of work

`items.update(key, expected_revision=revision, transform=callback)` reads and
checks the current row inside the write transaction, passes a detached model to
a synchronous callback, validates its result and replaces the row exactly once.
Missing/stale rows do not run the callback. Callback failures raise a value-free
`TransactionError`; invalid returned models raise `ValidationError`.

```python
from dataclasses import replace

with store.unit_of_work():
    project = projects.create(Project("garden", "Garden"))
    item = items.create(WorkItem("item-002", "Plan", project_key=project.key))
    items.update(item.key, expected_revision=item.revision,
                 transform=lambda value: replace(value, done=True))
```

All operations on this store's registered handles enlist in the same
`BEGIN IMMEDIATE` transaction. Returned rows inside the block are provisional
until successful context exit. A failed enlisted operation marks the entire
unit for rollback, even if the application catches its exception. Rollback
also releases newly reserved keys. Closing an active unit, nested units and
savepoints are refused. Other stores/connections do not enlist automatically.

Callbacks must not recurse into storage, await, start another connection owner
or perform external side effects. SQL rollback cannot undo application effects.
The store publishes no events; applications may publish only after the unit's
successful context exit. Independent connections see only committed data.

## Asynchronous ownership and cancellation

`AsyncApplicationStore` uses only the standard library. `open(path,
configure=callback, queue_limit=64, timeout=5.0)` starts one owned worker thread.
The synchronous configuration callback registers and initializes an
`ApplicationStore` there and returns application-selected handles/state.
`await facade.run(callback)` passes that state to a synchronous callback on the
same worker. `transactional=True` wraps the complete callback in a unit of work.

```python
async def save(facade, value):
    return await facade.run(lambda models: models["items"].create(value))
```

The connection, transactions and callbacks stay on that worker for their whole
lifetime. Keep callback inputs stable until completion and return detached
results, not connection-owned handles. Callbacks must be finite and obey the
same side-effect/reentrancy restrictions as synchronous transforms.

The queue admits 1–1024 pending jobs (default 64), excluding the active job.
Full admission raises `StorageBusyError` immediately; the application owns
retry/backpressure. A facade belongs to one event loop and process. Open a new
facade in each worker process rather than sharing it through a fork.

Cancellation before dispatch raises ordinary `asyncio.CancelledError` and skips
the job. After dispatch it raises `DispatchedCancellation`, a `CancelledError`
subclass with `receipt`. `await error.receipt.wait()` returns the actual result
or failure and may be retried after cancellation. A committed mutation is never
described as rolled back. Applications that discard a receipt must reload
before retrying a mutation. Cancelling a caller does not interrupt SQLite or
run a transform twice.

`await facade.aclose()` stops admission, drains accepted work, closes the
connection on its worker and waits for thread exit. Cancellation of a close
waiter leaves cleanup running; another `aclose()` can await it. Failed or
cancelled startup also closes its owned connection. `async with facade` provides
the same close behavior. Stop request admission before closing the facade.
