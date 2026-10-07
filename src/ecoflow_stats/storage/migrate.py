"""Applies versioned SQL migrations, in order, each tracked in ``user_version``.

v1 ships only ``0001_initial.sql``, so no migration path between delivery
slices is exercised yet — but the runner itself (file discovery, ordering,
and the version bump) is real and tested now, before a second migration
ever needs it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_SCHEMA_VERSION = 1

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _migration_files() -> list[tuple[int, Path]]:
    """Every ``NNNN_name.sql`` file under ``migrations/``, ordered by version."""
    files = []
    for path in _MIGRATIONS_DIR.glob("*.sql"):
        version_str = path.name.split("_", 1)[0]
        files.append((int(version_str), path))
    return sorted(files, key=lambda pair: pair[0])


def apply_pending_migrations(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Apply every migration newer than ``from_version``, in order.

    Each migration runs as its own script and bumps ``PRAGMA user_version``
    to that migration's number before the next one starts, so a failure
    partway through a future multi-migration run leaves the database at the
    last fully-applied version, not a mix of two.
    """
    for version, path in _migration_files():
        if version <= from_version:
            continue
        conn.executescript(path.read_text())
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


__all__ = ["CURRENT_SCHEMA_VERSION", "apply_pending_migrations"]
