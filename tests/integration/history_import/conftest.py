"""Shared synthetic-fixture helpers for the history-import integration
tests. No binary fixture and no real rows — a minimal, real schema-v1
`samples.db` is built with sqlite3 directly, matching the project's
testing strategy for legacy sources."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ecoflow_stats.devices.reading import FIELD_NAMES

V1_COLUMNS = ("ts", *FIELD_NAMES)


def write_valid_samples_db(path: Path) -> None:
    """A minimal, real schema-v1 `samples.db`: just the identity pragma
    and the 21-column `samples` table shape this module verifies."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version = 1")
        columns_sql = ", ".join(f"{name} REAL" for name in V1_COLUMNS)
        conn.execute(f"CREATE TABLE samples ({columns_sql})")
        conn.commit()
    finally:
        conn.close()


def write_outage_log(path: Path) -> None:
    path.write_text("2024-01-10 08:15:00\tcorte\t73\t\n2024-01-10 08:16:00\tretorno\t70\t1\n")
