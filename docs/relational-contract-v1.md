# Relational SQLite application contract, version 1

`relational-sqlite/1` is an adopted contract for BP-SQL-02 and subsequent owning
tasks. The [application store](application-storage.md) implements registered
scalar schemas, checked CRUD, transactions, typed queries and owned async work.
[Ordered upgrades and snapshots](sqlite-migrations.md) and the
[qualified one-host profile](sqlite-application-profile.md) complete the selected
SQLite slice. Existing
`Backpack` JSON-record APIs, database version 1, UUID identities, payload
migrations and 32-bit record revisions retain their current behavior. New
application domain revisions follow the separate
[shared contract](https://github.com/wispsy/compose_application/blob/main/docs/application-contracts-v1.md).

Run the [executable SQLite reference examples](../conformance_tests/test_relational_reference.py)
with `python -m unittest discover -s conformance_tests -v`. These examples prove
the selected SQL constraints/order/transaction expectations against synthetic
tables. Public-backend acceptance is in `tests/test_relational.py`, the
application transaction/query/async/migration suites and `process_tests/`.

## Registration and schema ownership

Registration explicitly supplies a namespace, trusted concrete model class,
codec, stable `type_key` and payload version, table name, primary-key field,
field-to-column mappings, nullability, unique groups, foreign keys and indexes.
One scalar primary key is required. The model and codec types must match;
duplicate model/type/table/column mappings are rejected atomically.
No table is discovered from an imported class name or stored type name.

Identifiers use ASCII `[a-z][a-z0-9_]{0,62}` and are quoted after registry
validation. Application tables start with `app_`; `records`, `tags`,
`records_type_id`, `tags_tag_record`, `sqlite_*` and library-owned `bp_*` names
are reserved. User fields cannot map to library metadata columns
`_storage_uuid`, `_revision`, `_payload_version`. Index names are explicit,
database-wide unique and owned by the registration. No supplied SQL fragments,
expressions, collations or functions become identifiers.

Registration alone performs no I/O or schema changes. An explicit
`initialize(namespace, schema_version=1)` boundary creates a registered fresh
schema transactionally, or validates the already-owned exact schema. It refuses
unowned existing application tables, incompatible metadata, unknown/newer schema
history and mismatched foreign-key targets without mutation. BP-SQL-02 owns
this initial boundary; BP-MIG-01 extends it with ordered upgrades and verified
backups. It cannot use a generic upgrade dependency to skip fresh-schema checks.

Namespace/model metadata, permanent key reservations and application migration
history use separate library-owned `bp_application_*` tables. They do not
repurpose `PRAGMA user_version` or existing JSON-record tables. The generic
migration facility must also support consumer-owned existing history names,
including Pretty Notebook's `pnbp_schema_migrations`; it must not silently
adopt or rewrite them. Only explicit migrations evolve an owned schema.

## First model and scalar slice

The pilot has a `Project` (`key`, unique `name`) and a `WorkItem`
(`key`, `title`, `done`, nullable `project_key`, nullable `rank`, nullable
`estimate`). Domain keys follow the shared immutable ASCII string-key profile.
Foreign keys target registered string primary keys in the same namespace.

| Declared scalar | Native validation and JSON | SQLite mapping |
| --- | --- | --- |
| `str` | Exact valid Unicode string; no implicit normalization | TEXT with binary comparison |
| `bool` | Exact bool, never an integer input | INTEGER with explicit 0/1 check; decode back to bool |
| `int` | Exact int, never bool; absolute value <= 9007199254740991 for portable JSON | INTEGER with the same range check |
| `float` | Finite native float; JSON load may accept finite numbers according to the codec | REAL; read converts to finite float |
| Nullable scalar | Explicit optional annotation plus nullable column; null is distinct from missing | SQL NULL |

Only these scalar field mappings are relational in version 1. Nested models,
containers, UUID/enum/date/decimal objects, composite keys, automatic joins,
cascades, generated fields and arbitrary SQL are refused even if a codec can
serialize them for JSON-record storage. Unsupported annotations/metadata fail
at registration; there is no silent JSON column fallback.

The initial preferred application profile uses `EnforceTypeCodec` on Python
3.14+ and its qualified public strict APIs. Explicit compatible codecs may
remain available within the same relational scalar slice. Check native values
before binding; do not rely on SQLite's affinity conversions as validation.
Validate database-loaded values through the codec too; corrupted rows raise a
value-free storage/validation error rather than returning a partial model.

Defaults belong to trusted model construction before full validation. Missing
required fields and unknown fields fail; omitted optional/default fields use
the model default, while supplied null is accepted only for a nullable field.
Do not let server ambient SQL defaults manufacture unvalidated model values.
Enforce primary/unique/foreign-key constraints in SQL as well as registration.
Nullable unique columns use the portable SQL behavior allowing multiple NULLs.
Foreign-key deletes use RESTRICT, with no cascades or deferred constraints.

## Identity, CRUD and detached values

The registered `ApplicationModel` handle provides these operations:

| Operation | Contract |
| --- | --- |
| `create(value)` | Validate; atomically reserve the domain key, assign a canonical internal storage UUID, insert revision 1 and return a detached row |
| `get(key)` | Return a detached model and domain key/string revision; missing row raises `NotFoundError` |
| `replace(key, value, expected_revision=...)` | Check immutable key, validate and conditionally replace all fields in one transaction; increment exactly once |
| `delete(key, expected_revision=...)` | Require the expected revision, enforce references and delete the row atomically; retain its permanent key reservation |
| `find(filters=..., order=..., limit=..., cursor=...)` | Typed bounded page under the query contract below |

Create never overwrites. Duplicate primary/unique keys map to
`AlreadyExistsError`; referential violations map to
`ConstraintError`, both typed `BackpackError` subclasses without submitted
values/SQL. BP-SQL-02 introduces these errors with implementation. Invalid
inputs use `ValidationError`, unknown model/schema uses `RegistrationError` or
`DatabaseVersionError`, missing keys use `NotFoundError`, stale checked writes
use `RevisionConflictError`, and operational SQLite failures use `StorageError`.

The storage UUID and application primary key are separately unique and stable.
Permanent `(namespace, type_key, key)` reservations prohibit deleted-key reuse,
so an old expected revision cannot accidentally modify a newly created object.
Reservations and insertion commit together; a failed create reserves nothing.
Deleting a referenced parent fails rather than deleting its children.

New application revisions are positive signed-64-bit counters represented as
canonical decimal strings on the public/wire boundary. They are distinct from
the legacy JSON-record revision type. Replace and delete require a revision;
no wildcard last-writer-wins mode is implicit. Overflow fails before mutation.
Missing-key detection takes precedence over revision conflict, under the same
owned transaction. An invalid/stale write preserves prior values, metadata,
revision and key reservations. No failed write publishes a change event.

Returned models/metadata/pages are detached from caller inputs, caches and each
other. Editing one cannot persist until a new checked write. Codec constructors
must be trusted and free of external side effects; validation/rollback cannot
undo arbitrary application effects.

## Typed bounded queries

- Only explicitly registered scalar fields are queryable. At most 16 ANDed
  predicates: `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `is_null`. Bool supports
  equality/membership only; ordered comparisons require a non-null str/int/float.
  `in` has 1–100 same-field typed values. Null comparisons use `is_null` with
  an exact bool; `eq None`, `ne None` and null membership are refused.
- `ne` and ordered comparisons follow SQL three-valued logic and exclude NULL
  rows. Select NULL explicitly using `is_null`. No cross-type coercion or raw
  SQL, OR, LIKE/regex, aggregate, join or expression is admitted.
- Values are bound parameters. Identifiers/operators come from validated
  registry/enum choices, never a value string. Unknown fields, mismatched types,
  unsupported operators and excess bounds fail before SQL execution.
- Supply 1–1000 rows per page (default 50), at most three explicit ascending or
  descending order fields. Every effective order puts NULL last and appends
  the domain key ascending unless already present, making equal values stable.
  Default order is domain key ascending. String order uses binary comparison.
- Use keyset pagination, not caller-controlled OFFSET. A versioned bounded
  cursor (at most 16 KiB) binds namespace/model/schema signature, canonical
  filter/order choices and the last effective sort tuple, including null ranks.
  Malformed, wrong-query or obsolete-schema cursors use `InvalidCursorError`.
  A cursor is not authorization, an encrypted payload or a snapshot lease;
  apply visibility/access policy again on each page and keep cursor values out
  of diagnostic output.
- Stable data yields each matching row once. Concurrent insertion/deletion or
  moving a sort value across the cursor may omit or repeat rows. A multi-page
  listing is not a frozen snapshot. Snapshot consumers need a separately owned
  read transaction or an explicit bounded resync.
- Declared indexes contain known scalar columns, in declared order. Create and
  validate them through the schema boundary. BP-QUERY-01 tests representative
  query plans, null/equal ordering, page continuation and injection-shaped values;
  it does not promise an index-only plan for every admitted predicate.

## Transactions, lifecycle and compatibility

Each operation owns one transaction/connection. BP-TXN-01 adds synchronous
`update(key, expected_revision, transform)` and multi-operation units of work:
read a detached current value inside the write transaction, check the revision,
run/validate the transform and commit or roll back everything. The initial unit
of work rejects nesting; savepoints need explicit later adoption. Failed
callbacks/validation and two-operation failures preserve prior data. Callbacks
must not perform external effects or recursively call another transaction owner.

Synchronous connections stay on their opening thread, close explicitly and use
bounded lock waiting with foreign keys enabled. BP-ASYNC-01 owns bounded queue,
worker/connection lifetime and cancellation outcomes; arbitrary per-call thread
offloading is not accepted. BP-CONC-01 qualifies one-host real-process startup,
conflicts, restart and visibility before any deployment claim. Events follow
commit; post-commit event failure is not a rollback.

The shared PostgreSQL subset will preserve typed values, null/constraint/identity
rules, optimistic revisions, detached values and query semantics. Driver,
pooling, isolation, lock retries, DDL, binary collation implementation and query
plans are backend-specific decisions for BP-PG-01. SQLite serialized writers,
PRAGMAs, file backups and local locks do not become PostgreSQL guarantees. No
PostgreSQL driver, hosted service, automatic migration or base dependency is
selected by this contract.
