"""SQLite connection lifecycle: identity and version guard, migrations, and
the single writer connection plus thread-local readers every other storage
module and service shares.

Concurrency model (design D4/D1): one writer connection, guarded by
``writer_lock`` for the collector, the derive job and POST actions; reader
connections are thread-local, one per thread that asks for one, used by web
request handlers.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import anyio

from ecoflow_stats.storage.migrate import CURRENT_SCHEMA_VERSION, apply_pending_migrations

_APPLICATION_ID = 0x45465354
""""EFST": identifies a file as an ecoflow-stats database. The schema
version alone cannot do this — the legacy ecoflow-panel ``samples.db`` also
sets ``user_version = 1``."""


class DatabaseError(Exception):
    """Base class for every database-open failure."""


class IncompatibleDatabaseError(DatabaseError):
    """The file is not an ecoflow-stats database, or its schema is newer
    than this build supports. Never raised after the file has been
    modified — the guard checks run before any write."""


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Session-level pragmas required on every connection; none of these
    persist into the file, so re-running them on each open is always safe."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()
    return bool(row[0])


def _open_and_migrate(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)

    if _has_tables(conn):
        (app_id,) = conn.execute("PRAGMA application_id").fetchone()
        if app_id != _APPLICATION_ID:
            conn.close()
            raise IncompatibleDatabaseError(f"{path} is not an ecoflow-stats database")

    (user_version,) = conn.execute("PRAGMA user_version").fetchone()
    if user_version > CURRENT_SCHEMA_VERSION:
        conn.close()
        raise IncompatibleDatabaseError(
            f"{path} was created by a newer version (schema {user_version});"
            " restore a backup or upgrade"
        )

    if user_version < CURRENT_SCHEMA_VERSION:
        # Reachable today only as 0 -> 1 (a brand-new file): there is no
        # data yet, so no pre-migration backup is needed. v1 ships exactly
        # one migration; backing up an older-but-nonzero schema before a
        # future migration is design-specified but not implemented here —
        # there is no second migration yet for a test to drive honestly.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        apply_pending_migrations(conn, from_version=user_version)

    return conn


class Database:
    """One identity- and version-guarded SQLite file: a single writer
    connection guarded by an async lock, and a thread-local read connection
    per thread that asks for one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.writer: sqlite3.Connection = _open_and_migrate(path)
        self.writer_lock = anyio.Lock()
        self._local = threading.local()

    def reader(self) -> sqlite3.Connection:
        """Return this thread's read connection, opening it on first use."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _configure_connection(conn)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        self.writer.close()
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def is_writable(self) -> bool:
        """Probe whether the writer connection can currently write, without
        persisting anything (health check, amendment A1): a collector still
        running against an unwritable database — a full disk, or a
        filesystem gone read-only — must report unhealthy."""
        try:
            self.writer.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        self.writer.rollback()
        return True


__all__ = ["Database", "DatabaseError", "IncompatibleDatabaseError"]
