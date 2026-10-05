"""Integration tests for the database open/identity/version guard.

Uses real SQLite files in ``tmp_path`` — milliseconds, and it catches SQL
errors an in-memory fake would hide.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ecoflow_stats.storage.database import (
    _APPLICATION_ID,
    Database,
    IncompatibleDatabaseError,
)
from ecoflow_stats.storage.migrate import CURRENT_SCHEMA_VERSION

_V1_TABLES = {
    "app_state",
    "devices",
    "app_runs",
    "samples",
    "fetch_failures",
    "import_runs",
    "legacy_outages",
    "derivations",
    "outage_events",
    "gaps",
    "decisions",
    "daily_rollups",
    "notifications",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_opening_a_fresh_path_creates_every_v1_table(tmp_path: Path) -> None:
    """A brand-new path gets the complete v1 schema, not a partial one."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        assert _V1_TABLES <= _table_names(db.writer)
    finally:
        db.close()


def test_opening_a_fresh_path_sets_identity_and_version(tmp_path: Path) -> None:
    """A fresh database is stamped as ours, at the current schema version —
    the guard that later refuses to open a foreign or newer file depends on
    this being set correctly on creation."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        (app_id,) = db.writer.execute("PRAGMA application_id").fetchone()
        (version,) = db.writer.execute("PRAGMA user_version").fetchone()
        assert app_id == _APPLICATION_ID
        assert version == CURRENT_SCHEMA_VERSION
    finally:
        db.close()


def test_reopening_an_existing_database_keeps_its_data(tmp_path: Path) -> None:
    """Closing and reopening the same file must not re-run migrations
    destructively or lose rows already written."""
    path = tmp_path / "ecoflow-stats.db"
    db = Database(path)
    db.writer.execute(
        "INSERT INTO devices (sn, adapter_id, created_at) VALUES ('ABC123', 'generic', 1)"
    )
    db.writer.commit()
    db.close()

    db2 = Database(path)
    try:
        (count,) = db2.writer.execute("SELECT COUNT(*) FROM devices").fetchone()
        assert count == 1
    finally:
        db2.close()


def test_a_file_with_a_foreign_application_id_is_refused_without_modification(
    tmp_path: Path,
) -> None:
    """A SQLite file that happens to have tables but a different
    ``application_id`` (for example, the panel's own ``samples.db``, which
    also sets ``user_version = 1`` — version alone cannot tell them apart)
    must be refused, and must not be altered by the attempt."""
    path = tmp_path / "foreign.db"
    foreign = sqlite3.connect(path)
    foreign.execute("PRAGMA application_id = 999")
    foreign.execute("PRAGMA user_version = 1")
    foreign.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    foreign.commit()
    foreign.close()
    before = path.read_bytes()

    with pytest.raises(IncompatibleDatabaseError):
        Database(path)

    assert path.read_bytes() == before


def test_a_newer_schema_version_is_refused_without_modification(tmp_path: Path) -> None:
    """A database stamped with a schema version newer than this build
    supports must refuse to open rather than silently downgrade or alter it."""
    path = tmp_path / "ecoflow-stats.db"
    db = Database(path)
    db.close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    before = path.read_bytes()

    with pytest.raises(IncompatibleDatabaseError):
        Database(path)

    assert path.read_bytes() == before


def test_reader_connections_are_thread_local_and_distinct_from_the_writer(
    tmp_path: Path,
) -> None:
    """``reader()`` must not hand out the single writer connection — mixing
    them would defeat the point of having thread-local read connections at
    all (a long-running read transaction could then block a write, or vice
    versa)."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        reader = db.reader()
        assert reader is not db.writer
        assert db.reader() is reader  # cached per-thread, not reopened every call
    finally:
        db.close()
