"""The ``fetch_failures`` table: a minute without a sample while the app
was running, with a reason category — so later analysis can distinguish
"nothing happened" from "collection failed" (acquisition requirement:
fetch outcomes are recorded)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

FailureOutcome = Literal["timeout", "network", "http", "api", "bad_payload", "internal", "store"]


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """One failed collection attempt."""

    ts: int
    outcome: FailureOutcome
    code: str | None
    attempts: int
    latency_ms: int | None


class FailureLog:
    """Synchronous ``fetch_failures`` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, device_id: int, failure: FetchFailure) -> None:
        """Persist one fetch failure; a duplicate for the same device and
        minute (for example a retried write) is ignored, not duplicated."""
        self._conn.execute(
            "INSERT OR IGNORE INTO fetch_failures"
            " (device_id, ts, outcome, code, attempts, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                device_id,
                failure.ts,
                failure.outcome,
                failure.code,
                failure.attempts,
                failure.latency_ms,
            ),
        )
        self._conn.commit()

    def between(self, device_id: int, start: int, end: int) -> list[FetchFailure]:
        """Return a device's failures within ``[start, end]``, in timestamp order."""
        rows = self._conn.execute(
            "SELECT ts, outcome, code, attempts, latency_ms FROM fetch_failures"
            " WHERE device_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (device_id, start, end),
        ).fetchall()
        return [
            FetchFailure(
                ts=row["ts"],
                outcome=row["outcome"],
                code=row["code"],
                attempts=row["attempts"],
                latency_ms=row["latency_ms"],
            )
            for row in rows
        ]


__all__ = ["FailureLog", "FailureOutcome", "FetchFailure"]
