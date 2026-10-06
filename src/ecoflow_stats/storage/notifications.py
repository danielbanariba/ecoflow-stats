"""The `notifications` table: an at-least-once alert ledger keyed by
(device, event start, kind), so a restart replay can never duplicate an
already-sent outage-start or outage-end alert (notifications requirement:
"No Duplicate Notifications Across Restarts"; design D10).

`claim()`'s `INSERT OR IGNORE` on the table's own primary key is the only
de-duplication mechanism -- there is no secondary in-memory check that
could drift from what the database actually holds. `due()` also expires a
pending alert that has been retried for too long (6 hours, design D10)
before returning the still-retryable ones.
"""

from __future__ import annotations

import sqlite3

from ecoflow_stats.notifications.messages import AlertKey, PendingAlert

_EXPIRE_AFTER_S = 6 * 3600
"""Design D10: "retried each tick with backoff for up to 6 hours, then
expired" -- an unreachable ntfy server must not be retried forever."""


class NotificationLedger:
    """Synchronous `notifications` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def claim(self, key: AlertKey, title: str, body: str, now: int) -> bool:
        """Claim this alert slot; return `False` if it was already
        claimed (by an earlier live transition or a replay of one)."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO notifications"
            " (device_id, event_start_ts, kind, status, title, body, created_at, attempts)"
            " VALUES (?, ?, ?, 'pending', ?, ?, ?, 0)",
            (key.device_id, key.event_start_ts, key.kind, title, body, now),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def due(self, now: int) -> list[PendingAlert]:
        """Expire every pending alert older than the retry window, then
        return every alert still pending a (re)delivery attempt."""
        self._conn.execute(
            "UPDATE notifications SET status = 'expired'"
            " WHERE status = 'pending' AND ? - created_at >= ?",
            (now, _EXPIRE_AFTER_S),
        )
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT device_id, event_start_ts, kind, title, body, attempts"
            " FROM notifications WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [
            PendingAlert(
                key=AlertKey(
                    device_id=row["device_id"],
                    event_start_ts=row["event_start_ts"],
                    kind=row["kind"],
                ),
                title=row["title"],
                body=row["body"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def mark_sent(self, key: AlertKey, now: int) -> None:
        self._conn.execute(
            "UPDATE notifications SET status = 'sent', sent_at = ?"
            " WHERE device_id = ? AND event_start_ts = ? AND kind = ?",
            (now, key.device_id, key.event_start_ts, key.kind),
        )
        self._conn.commit()

    def mark_failed(self, key: AlertKey, error: str, now: int) -> None:
        """`now` is part of the `NotificationLedger` port's shape; this
        table has no per-attempt timestamp column to write it to."""
        self._conn.execute(
            "UPDATE notifications SET attempts = attempts + 1, last_error = ?"
            " WHERE device_id = ? AND event_start_ts = ? AND kind = ?",
            (error, key.device_id, key.event_start_ts, key.kind),
        )
        self._conn.commit()


__all__ = ["NotificationLedger"]
