"""Explicit namespace-owned ordered SQLite upgrades with verified backups."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import inspect
import math
from pathlib import Path
import re
import sqlite3

from .errors import BackpackError, DatabaseVersionError, RegistrationError, SchemaMigrationError, ValidationError
from .records import utc_now
from .relational import _identifier, _quote
from .snapshots import SnapshotReceipt, snapshot_sqlite

_OWNERS = """CREATE TABLE bp_migration_owners (
    namespace TEXT PRIMARY KEY NOT NULL, history_table TEXT NOT NULL,
    scoped INTEGER NOT NULL CHECK(scoped IN (0,1)))"""


@dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationHistory:
    table: str = "bp_schema_migrations"
    scoped: bool = True


@dataclass(frozen=True)
class MigrationResult:
    version: int
    backup: SnapshotReceipt | None


def _plan(namespace, steps, history):
    _identifier(namespace)
    if (not isinstance(history, MigrationHistory) or type(history.scoped) is not bool
            or history.table in {"records", "tags", "bp_migration_owners", "bp_application_keys", "bp_application_namespaces"}):
        raise RegistrationError("select a distinct explicit migration history table")
    _identifier(history.table)
    if type(steps) not in (tuple, list) or not 1 <= len(steps) <= 1000:
        raise RegistrationError("provide a finite complete ordered migration plan")
    steps = tuple(steps)
    for version, step in enumerate(steps, 1):
        if (not isinstance(step, MigrationStep) or type(step.version) is not int or step.version != version
                or type(step.name) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", step.name)
                or not callable(step.apply) or inspect.iscoroutinefunction(step.apply)):
            raise RegistrationError("migration steps require contiguous versions, stable names and synchronous callbacks")
    if len({step.name for step in steps}) != len(steps):
        raise RegistrationError("migration names must be unique within a namespace")
    return steps


def _exists(connection, name):
    return connection.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='table'", (name,)).fetchone()


def _history_ddl(history):
    prefix = "namespace TEXT NOT NULL," if history.scoped else ""
    primary = "PRIMARY KEY(namespace,version)" if history.scoped else "PRIMARY KEY(version)"
    return (f"CREATE TABLE {_quote(history.table)} ({prefix}version INTEGER NOT NULL,name TEXT NOT NULL,"
            f"applied_at TEXT NOT NULL,backup_path TEXT,backup_sha256 TEXT,{primary})")


def _history_rows(connection, namespace, history):
    columns = {row[1]: row for row in connection.execute(f"PRAGMA table_info({_quote(history.table)})")}
    required = {"version", "name", "applied_at", "backup_path", "backup_sha256"}
    if history.scoped:
        required.add("namespace")
    if (not required <= columns.keys() or columns["version"][2].upper() != "INTEGER"
            or columns["version"][5] != (2 if history.scoped else 1)
            or (history.scoped and columns["namespace"][5] != 1)):
        raise DatabaseVersionError("migration history schema is incompatible")
    where = " WHERE namespace=?" if history.scoped else ""
    return connection.execute(
        f"SELECT version,name,applied_at,backup_path,backup_sha256 FROM {_quote(history.table)}{where} ORDER BY version",
        (namespace,) if history.scoped else (),
    ).fetchall()


def _validate_history(rows, steps):
    if len(rows) > len(steps):
        raise DatabaseVersionError("migration history is newer than the selected plan")
    for version, row in enumerate(rows, 1):
        if type(row[0]) is not int or row[0] != version or row[1] != steps[version-1].name:
            raise DatabaseVersionError("migration history has unknown versions, gaps or changed names")
        try:
            if type(row[2]) is not str or datetime.fromisoformat(row[2].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError
            if (row[3] is None) != (row[4] is None):
                raise ValueError
            if row[3] is not None and (type(row[3]) is not str or not row[3] or type(row[4]) is not str or not re.fullmatch(r"[a-f0-9]{64}", row[4])):
                raise ValueError
        except (TypeError, ValueError):
            raise DatabaseVersionError("migration history has malformed timestamps or backup receipts") from None


def _invoke(connection, callback, *args):
    # Trusted migrations still cannot accidentally commit through executescript,
    # change connection ownership or escape the runner's transaction boundary.
    def authorize(action, first, second, database, trigger):
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)
    try:
        result = callback(connection, *args)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise SchemaMigrationError("migration callbacks must finish synchronously")
    except BackpackError:
        raise
    except Exception:
        raise SchemaMigrationError("migration callback or validation failed") from None
    finally:
        connection.set_authorizer(None)


def _insert_history(connection, namespace, history, step, receipt):
    columns = "version,name,applied_at,backup_path,backup_sha256"
    values = [step.version, step.name, utc_now(), str(receipt.path) if receipt else None, receipt.sha256 if receipt else None]
    if history.scoped:
        columns = "namespace," + columns
        values.insert(0, namespace)
    connection.execute(f"INSERT INTO {_quote(history.table)} ({columns}) VALUES ({','.join('?' for _ in values)})", values)


def _migrate_connection(connection, source, *, namespace, steps, history, backup_directory,
                        adopt_history=False, validate=None, baseline=0, prepare=None, finish=None):
    steps = _plan(namespace, steps, history)
    if connection.in_transaction:
        raise SchemaMigrationError("migration requires an idle owned connection")
    try:
        connection.execute("BEGIN IMMEDIATE")
        owner_table = _exists(connection, "bp_migration_owners")
        if owner_table is not None and owner_table[0] != _OWNERS:
            raise DatabaseVersionError("migration namespace ownership metadata is incompatible")
        owner = None if owner_table is None else connection.execute(
            "SELECT history_table,scoped FROM bp_migration_owners WHERE namespace=?", (namespace,)).fetchone()
        if owner is not None and tuple(owner) != (history.table, int(history.scoped)):
            raise DatabaseVersionError("namespace already owns a different migration history")
        if owner_table is not None and not history.scoped and connection.execute(
                "SELECT 1 FROM bp_migration_owners WHERE history_table=? AND namespace!=?", (history.table, namespace)).fetchone():
            raise DatabaseVersionError("unscoped consumer history cannot be shared between namespaces")
        existing = _exists(connection, history.table)
        rows = [] if existing is None else _history_rows(connection, namespace, history)
        _validate_history(rows, steps)
        if owner is None and existing is not None and (rows or not history.scoped or owner_table is None) and not adopt_history:
            raise DatabaseVersionError("existing consumer history requires explicit adoption")
        if owner is not None and not rows:
            raise DatabaseVersionError("owned migration history is missing or incomplete")
        current = len(rows) if rows else baseline
        if prepare is not None:
            _invoke(connection, prepare, current)
        if validate is not None:
            _invoke(connection, validate, current)
        pending = steps[current:]
        receipt = None
        if pending and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
            receipt = snapshot_sqlite(source, backup_directory, prefix=f"{namespace}-from-v{current}")
        if owner is None:
            if owner_table is None:
                connection.execute(_OWNERS)
            connection.execute("INSERT INTO bp_migration_owners VALUES (?,?,?)", (namespace, history.table, int(history.scoped)))
        if existing is None:
            connection.execute(_history_ddl(history))
        if not rows and baseline:
            for step in steps[:baseline]:
                _insert_history(connection, namespace, history, step, receipt)
        for step in pending:
            _invoke(connection, step.apply)
            if validate is not None:
                _invoke(connection, validate, step.version)
            _insert_history(connection, namespace, history, step, receipt)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaMigrationError("migrated schema failed foreign-key validation")
        if finish is not None:
            _invoke(connection, finish, len(steps))
        connection.commit()
        return MigrationResult(len(steps), receipt)
    except BaseException as error:
        connection.rollback()
        if isinstance(error, sqlite3.Error):
            raise SchemaMigrationError("SQLite schema migration failed and rolled back") from None
        raise


def migrate_sqlite(source, *, namespace: str, steps, backup_directory,
                   history=MigrationHistory(), adopt_history=False, validate=None, timeout=5.0) -> MigrationResult:
    """Run a complete selected plan; consumer history is adopted only explicitly."""
    _plan(namespace, steps, history)
    if type(adopt_history) is not bool or (validate is not None and not callable(validate)):
        raise ValidationError("select explicit history adoption and a synchronous validation callback")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
        raise ValidationError("migration lock timeout must be between 0 and 60 seconds")
    path = Path(source).absolute()
    if path.is_symlink():
        raise ValidationError("migration source must not be a symlink")
    try:
        with closing(sqlite3.connect(path, timeout=timeout)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            return _migrate_connection(connection, path, namespace=namespace, steps=steps, history=history,
                                       backup_directory=backup_directory, adopt_history=adopt_history, validate=validate)
    except sqlite3.Error:
        raise SchemaMigrationError("could not open the explicit migration database") from None
