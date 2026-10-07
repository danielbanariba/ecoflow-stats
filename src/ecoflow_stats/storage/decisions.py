"""The `decisions` table: durable user verdicts on a gap or a legacy
outage event, anchored to the target's time interval so they survive
every recompute (design D4; storage requirement: "A User Decision on a
Gap Is Durable and Survives Recomputation").

A new decision on the same `(device_id, target, start_ts, end_ts)`
supersedes the previously active one -- `active()` only ever returns
rows with `superseded_by IS NULL`, matching the `decisions_active`
partial index the schema already defines. `undo()` marks a decision
superseded by itself, which satisfies the same `superseded_by IS NULL`
filter without a DELETE, keeping the full audit trail (design D4:
"undo/change keeps the audit trail").
"""

from __future__ import annotations

import sqlite3

from ecoflow_stats.outages.model import Decision

_SELECT_COLUMNS = "id, device_id, target, start_ts, end_ts, verdict, decided_at, superseded_by"


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        device_id=row["device_id"],
        target=row["target"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        verdict=row["verdict"],
        decided_at=row["decided_at"],
        superseded_by=row["superseded_by"],
    )


class DecisionStore:
    """Synchronous `decisions` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, decision: Decision) -> int:
        """Insert a new decision, then supersede the previously active
        one (if any) for the same device/target/interval. Returns the
        new row's id."""
        cursor = self._conn.execute(
            "INSERT INTO decisions (device_id, target, start_ts, end_ts, verdict, decided_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                decision.device_id,
                decision.target,
                decision.start_ts,
                decision.end_ts,
                decision.verdict,
                decision.decided_at,
            ),
        )
        new_id = cursor.lastrowid
        assert new_id is not None
        self._conn.execute(
            "UPDATE decisions SET superseded_by = ?"
            " WHERE device_id = ? AND target = ? AND start_ts = ? AND end_ts = ?"
            " AND superseded_by IS NULL AND id != ?",
            (
                new_id,
                decision.device_id,
                decision.target,
                decision.start_ts,
                decision.end_ts,
                new_id,
            ),
        )
        self._conn.commit()
        return new_id

    def active(self, device_id: int) -> list[Decision]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM decisions"
            " WHERE device_id = ? AND superseded_by IS NULL ORDER BY id",
            (device_id,),
        ).fetchall()
        return [_row_to_decision(row) for row in rows]

    def undo(self, decision_id: int) -> None:
        """Mark a decision superseded by itself: undone, never replaced,
        and never deleted."""
        self._conn.execute(
            "UPDATE decisions SET superseded_by = id WHERE id = ? AND superseded_by IS NULL",
            (decision_id,),
        )
        self._conn.commit()


__all__ = ["DecisionStore"]
