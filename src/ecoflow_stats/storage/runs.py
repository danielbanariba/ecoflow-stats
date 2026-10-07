"""The ``app_runs`` table: when the application itself was running, so a
gap's cause can say "the app was down" rather than silently attributing a
window with no app-side explanation to the EcoFlow cloud."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppRun:
    """One run of the application process."""

    id: int
    started_at: int
    last_tick_at: int | None
    stopped_at: int | None
    app_version: str


class RunLog:
    """Synchronous ``app_runs`` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, started_at: int, app_version: str) -> int:
        """Record a new run starting now; return its id."""
        cursor = self._conn.execute(
            "INSERT INTO app_runs (started_at, app_version) VALUES (?, ?)",
            (started_at, app_version),
        )
        self._conn.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def tick(self, run_id: int, at: int) -> None:
        """Record that the collector is still alive, as of ``at``."""
        self._conn.execute("UPDATE app_runs SET last_tick_at = ? WHERE id = ?", (at, run_id))
        self._conn.commit()

    def stop(self, run_id: int, at: int) -> None:
        """Record a clean shutdown at ``at``."""
        self._conn.execute("UPDATE app_runs SET stopped_at = ? WHERE id = ?", (at, run_id))
        self._conn.commit()

    def covering(self, start: int, end: int) -> list[AppRun]:
        """Every run whose ``[started_at, stopped_at-or-last_tick_at]``
        interval overlaps ``[start, end]`` — used by gap evidence to decide
        whether a silent window is explained by the app itself being down."""
        rows = self._conn.execute(
            "SELECT id, started_at, last_tick_at, stopped_at, app_version"
            " FROM app_runs"
            " WHERE started_at <= ? AND COALESCE(stopped_at, last_tick_at, started_at) >= ?"
            " ORDER BY started_at",
            (end, start),
        ).fetchall()
        return [
            AppRun(
                id=row["id"],
                started_at=row["started_at"],
                last_tick_at=row["last_tick_at"],
                stopped_at=row["stopped_at"],
                app_version=row["app_version"],
            )
            for row in rows
        ]


__all__ = ["AppRun", "RunLog"]
