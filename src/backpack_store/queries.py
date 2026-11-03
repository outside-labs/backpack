"""Finite parameterized scalar predicates and query-bound keyset cursors."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from typing import Generic, TypeVar

from .errors import InvalidCursorError, RegistrationError, ValidationError
from .relational import ApplicationRow, _key, _quote

T = TypeVar("T")


@dataclass(frozen=True)
class Filter:
    field: str
    operator: str
    value: object


@dataclass(frozen=True)
class Order:
    field: str
    descending: bool = False


@dataclass(frozen=True)
class ApplicationPage(Generic[T]):
    rows: tuple[ApplicationRow[T], ...]
    next_cursor: str | None


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _prepare(model, filters, order, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValidationError("query limit must be between 1 and 1000")
    if type(filters) not in (tuple, list) or len(filters) > 16:
        raise ValidationError("queries admit at most 16 predicates")
    if type(order) not in (tuple, list) or len(order) > 3:
        raise ValidationError("queries admit at most three explicit order fields")
    clauses, parameters, canonical = [], [], []
    operators = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
    for predicate in filters:
        if not isinstance(predicate, Filter) or type(predicate.field) is not str or predicate.field not in model._by_field:
            raise ValidationError("query predicate must name a registered field")
        column = model._by_field[predicate.field]
        name, operation, value = _quote(column.name), predicate.operator, predicate.value
        if type(operation) is not str or operation not in (*operators, "in", "is_null"):
            raise ValidationError("query operator is unsupported")
        if operation == "is_null":
            if type(value) is not bool:
                raise ValidationError("is_null requires an exact bool")
            clauses.append(f"{name} IS {'NULL' if value else 'NOT NULL'}")
        elif operation == "in":
            if type(value) not in (tuple, list) or not 1 <= len(value) <= 100:
                raise ValidationError("membership requires 1 to 100 typed values")
            for member in value:
                if member is None:
                    raise ValidationError("null membership is unsupported; use is_null")
                column.check(member)
            value = sorted({_json(member): member for member in value}.values(), key=_json)
            clauses.append(f"{name} IN ({','.join('?' for _ in value)})")
            parameters.extend(value)
        else:
            if value is None:
                raise ValidationError("null comparisons require is_null")
            column.check(value)
            if column.scalar is bool and operation not in ("eq", "ne"):
                raise ValidationError("bool predicates support equality and membership only")
            clauses.append(f"{name}{operators[operation]}?")
            parameters.append(value)
        canonical.append([predicate.field, operation, value])
    effective = []
    for item in order:
        if (not isinstance(item, Order) or type(item.field) is not str or item.field not in model._by_field
                or type(item.descending) is not bool or item.field in [field for field, _ in effective]):
            raise ValidationError("query order requires distinct registered fields and exact directions")
        effective.append((item.field, item.descending))
    if model._primary_key not in [field for field, _ in effective]:
        effective.append((model._primary_key, False))
    signature = model._store._schemas[model._namespace]
    descriptor = [model._namespace, model._codec.type_key, signature,
                  sorted(canonical, key=_json), effective]
    fingerprint = hashlib.sha256(_json(descriptor).encode()).hexdigest()
    ordering = []
    for field, descending in effective:
        column = model._by_field[field]
        name = _quote(column.name)
        if column.nullable:
            ordering.append(f"{name} IS NULL ASC")
        ordering.append(f"{name} {'DESC' if descending else 'ASC'}")
    return clauses, parameters, effective, ordering, fingerprint


def _unique_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError
        value[key] = child
    return value


def _read_cursor(cursor, fingerprint, model, effective):
    try:
        if type(cursor) is not str or not 1 <= len(cursor) <= 16_384:
            raise ValueError
        decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
        data = json.loads(decoded, object_pairs_hook=_unique_pairs)
        if (type(data) is not dict or set(data) != {"version", "query", "last"}
                or type(data["version"]) is not int or data["version"] != 1
                or data["query"] != fingerprint or type(data["last"]) is not list
                or len(data["last"]) != len(effective)):
            raise ValueError
        for value, (field, _) in zip(data["last"], effective, strict=True):
            model._by_field[field].check(value)
            if field == model._primary_key:
                _key(value)
        return data["last"]
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise InvalidCursorError("cursor is malformed or does not match this model/schema/query") from None


def _continuation(model, effective, last):
    alternatives, parameters, equal, equal_parameters = [], [], [], []
    for (field, descending), value in zip(effective, last, strict=True):
        column = model._by_field[field]
        name = _quote(column.name)
        if value is not None:
            comparison = f"{name}{'<' if descending else '>'}?"
            if column.nullable:
                comparison = f"({comparison} OR {name} IS NULL)"
            alternatives.append("(" + " AND ".join([*equal, comparison]) + ")")
            parameters.extend([*equal_parameters, value])
            equal.append(f"{name}=?")
            equal_parameters.append(value)
        else:
            equal.append(f"{name} IS NULL")
    return "(" + " OR ".join(alternatives) + ")", parameters


def find(model, *, filters=(), order=(), limit=50, cursor=None):
    model._store._connection()
    if model._namespace not in model._store._initialized:
        raise RegistrationError("initialize the namespace before querying rows")
    clauses, parameters, effective, ordering, fingerprint = _prepare(model, filters, order, limit)
    if cursor is not None:
        last = _read_cursor(cursor, fingerprint, model, effective)
        continuation, values = _continuation(model, effective, last)
        clauses.append(continuation)
        parameters.extend(values)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with model._store._operation(model) as connection:
        raw = connection.execute(f"SELECT * FROM {_quote(model._table)}{where} ORDER BY {','.join(ordering)} LIMIT ?",
                                 [*parameters, limit + 1]).fetchall()
        rows = tuple(model._row(connection, row) for row in raw[:limit])
    next_cursor = None
    if len(raw) > limit:
        last = [getattr(rows[-1].value, field) for field, _ in effective]
        next_cursor = base64.urlsafe_b64encode(_json({"version": 1, "query": fingerprint, "last": last}).encode()).decode("ascii")
        if len(next_cursor) > 16_384:
            raise ValidationError("sort values exceed the bounded cursor profile; use a shorter order field")
    return ApplicationPage(rows, next_cursor)
