"""Private consistent SQLite snapshots, including committed WAL state."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from .errors import SnapshotError, ValidationError


@dataclass(frozen=True)
class SnapshotReceipt:
    path: Path
    sha256: str
    size_bytes: int


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise SnapshotError("SQLite snapshot paths must name regular files without symlinks")


def _sync_directory(directory):
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def snapshot_sqlite(source, directory, *, prefix="snapshot", timeout=30.0) -> SnapshotReceipt:
    """Create a new private, integrity-checked snapshot; never overwrite a file."""
    if type(prefix) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,127}", prefix):
        raise ValidationError("snapshot prefix must use bounded lowercase ASCII")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValidationError("snapshot timeout must be finite and between 0 and 300 seconds")
    path = None
    try:
        source, directory = Path(source).absolute(), Path(directory).absolute()
        _regular(source)
        if directory.is_symlink():
            raise SnapshotError("snapshot directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode) or (os.name != "nt" and stat.S_IMODE(mode) & 0o077):
            raise SnapshotError("snapshot directory must be a private owned directory")
        descriptor, filename = tempfile.mkstemp(prefix=prefix + "-", suffix=".sqlite3", dir=directory)
        os.close(descriptor)
        path = Path(filename)
        deadline = time.monotonic() + timeout
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise SnapshotError("SQLite snapshot exceeded its configured time bound")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=timeout)) as original:
            with closing(sqlite3.connect(path)) as destination:
                original.backup(destination, pages=128, progress=progress, sleep=0.01)
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise SnapshotError("SQLite snapshot failed integrity validation")
        _regular(path)
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
        _sync_directory(directory)
        return SnapshotReceipt(path, _digest(path), path.stat().st_size)
    except BaseException as error:
        if path is not None:
            path.unlink(missing_ok=True)
        if not isinstance(error, Exception):
            raise
        if isinstance(error, SnapshotError):
            raise
        raise SnapshotError("could not create and verify a private SQLite snapshot") from None


def verify_snapshot(receipt: SnapshotReceipt) -> None:
    """Verify a retained receipt without modifying or restoring its database."""
    try:
        if not isinstance(receipt, SnapshotReceipt):
            raise ValueError
        _regular(receipt.path)
        if receipt.path.stat().st_size != receipt.size_bytes or _digest(receipt.path) != receipt.sha256:
            raise ValueError
        with closing(sqlite3.connect(receipt.path.absolute().as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError
    except Exception:
        raise SnapshotError("SQLite snapshot does not match its verified receipt") from None
