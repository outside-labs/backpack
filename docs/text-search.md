# Registered literal text search

`ApplicationModel.find(search=TextSearch(("title", "summary"), "needle"),
filters=(Filter("done", "eq", False),), order=(Order("title"),), limit=50)`
queries registered `str` columns, including nullable text. The fields form an
OR, combined with existing typed AND predicates. `instr(column, ?)` implements
case-sensitive Unicode substring matching without normalization; percent,
underscore, quote, backslash, NUL and SQL-shaped text are bound literal data.
Null text does not match. No full-text engine or additional dependency is used.

`TextSearch` admits 1–8 distinct text field names and 256 term code points.
Field order canonicalizes; empty text clears search, while still validating
the supplied fields. Search descriptor/field set joins the existing schema,
predicate and order cursor identity. Changed search refuses old continuation.
Empty search retains the original no-search cursor identity. Page size may
change without resetting the storage cursor. Existing deterministic ascending
primary-key tie breaking and null-last ordering remain in either direction.

`max_work=1000000` limits approximate SQLite VM instructions per `find` call,
including SQL row hydration. Applications may select a smaller positive budget.
`QueryBudgetError` is a redacted failure with no partial page. Its progress
handler is removed before transaction completion, and the connection remains
usable. This is separate from the maximum 1,000 returned rows; unindexed search
and sorting may visit the whole declared dataset. The instruction limit is not
a deadline or hard CPU/memory sandbox, and one SQLite opcode/string operation
can itself perform work. Registered query ownership reserves the connection's
progress handler for the duration of `find`.

Use the existing AsyncApplicationStore worker for async callers. Queued
cancellation skips dispatch; cancellation after dispatch retains an operation
receipt and cannot promise rollback. Reads return detached values and close
drains the owned worker. Each new query sees committed writes from independent
processes/reopens. Keyset pages across concurrent edits are fresh reads, not a
frozen multi-page snapshot; refresh explicitly when a stable new view is needed.
The record-store API and previous non-search cursor profile are unchanged.

Contract: [tabular/1](https://github.com/wispsy/compose_application/blob/d8c4e3a1d4cd1979222c0dddcbf0a14d44ee415c/docs/tabular-v1.md).
