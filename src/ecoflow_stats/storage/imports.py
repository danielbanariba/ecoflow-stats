"""The `import_runs` table: one row per execution of the history-import CLI
command, recording when it ran, the source timezone it was given, and —
once finished — a JSON report of what it did."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImportRun:
    """One row of the `import_runs` table."""

    id: int
    device_id: int
    started_at: int
    finished_at: int | None
    source_tz: str | None
    report_json: str | None


def _row_to_run(row: sqlite3.Row) -> ImportRun:
    return ImportRun(
        id=row["id"],
        device_id=row["device_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        source_tz=row["source_tz"],
        report_json=row["report_json"],
    )


class ImportRunStore:
    """Synchronous `import_runs` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, device_id: int, started_at: int, source_tz: str) -> int:
        """Record a new import run starting now; return its id. Every
        imported `legacy_outages` row from this run references this id, so
        the import's own history stays auditable."""
        cursor = self._conn.execute(
            "INSERT INTO import_runs (device_id, started_at, source_tz) VALUES (?, ?, ?)",
            (device_id, started_at, source_tz),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def finish(self, run_id: int, finished_at: int, report_json: str) -> None:
        """Record a run's completion and its report."""
        self._conn.execute(
            "UPDATE import_runs SET finished_at = ?, report_json = ? WHERE id = ?",
            (finished_at, report_json, run_id),
        )
        self._conn.commit()

    def get(self, run_id: int) -> ImportRun | None:
        row = self._conn.execute(
            "SELECT id, device_id, started_at, finished_at, source_tz, report_json"
            " FROM import_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return _row_to_run(row) if row is not None else None


__all__ = ["ImportRun", "ImportRunStore"]
