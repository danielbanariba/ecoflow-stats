"""The `legacy_outages` table: `outages.log` events imported as provenance.

A row is insert-or-ignore on its `(device_id, start_ts)` key, with exactly
one allowed update: closing an event that was still open (`end_ts IS NULL`)
at an earlier import, because the legacy watcher kept appending to
`outages.log` during the parallel-run transition. Every other field is
fixed at first insert — re-importing an already-closed event changes
nothing (history-import requirement: idempotent and re-runnable).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacyOutage:
    """One row of the `legacy_outages` table."""

    id: int
    device_id: int
    start_ts: int
    end_ts: int | None
    soc_start: int | None
    soc_end: int | None
    logged_minutes: int | None
    start_line: str | None
    end_line: str | None
    source_tz: str
    flags: frozenset[str]
    import_id: int


_SELECT_COLUMNS = (
    "id, device_id, start_ts, end_ts, soc_start, soc_end, logged_minutes,"
    " start_line, end_line, source_tz, flags, import_id"
)


def _row_to_outage(row: sqlite3.Row) -> LegacyOutage:
    return LegacyOutage(
        id=row["id"],
        device_id=row["device_id"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        soc_start=row["soc_start"],
        soc_end=row["soc_end"],
        logged_minutes=row["logged_minutes"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        source_tz=row["source_tz"],
        flags=frozenset(row["flags"].split()),
        import_id=row["import_id"],
    )


class LegacyStore:
    """Synchronous `legacy_outages` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        *,
        device_id: int,
        start_ts: int,
        end_ts: int | None,
        soc_start: int | None,
        soc_end: int | None,
        logged_minutes: int | None,
        start_line: str | None,
        end_line: str | None,
        source_tz: str,
        flags: frozenset[str],
        import_id: int,
    ) -> bool:
        """Insert a new legacy event, or — only when the stored row is
        still open — close it with this call's end fields. Matches the
        design's documented guard exactly: every other column is
        insert-or-ignore, never overwritten by a later import.

        Returns whether this call actually changed anything (a fresh
        insert, or an allowed close-the-open-event update) as opposed
        to a true no-op re-import of an already-present, already-closed
        row -- CLI-02 (qa-report-data-01.md): the import report needs
        this to tell "inserted" apart from "already present", instead
        of always claiming every parsed event as newly "imported"."""
        cursor = self._conn.execute(
            """
            INSERT INTO legacy_outages
                (device_id, start_ts, end_ts, soc_start, soc_end, logged_minutes,
                 start_line, end_line, source_tz, flags, import_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, start_ts) DO UPDATE SET
                end_ts = excluded.end_ts,
                soc_end = excluded.soc_end,
                end_line = excluded.end_line,
                logged_minutes = excluded.logged_minutes
            WHERE legacy_outages.end_ts IS NULL AND excluded.end_ts IS NOT NULL
            """,
            (
                device_id,
                start_ts,
                end_ts,
                soc_start,
                soc_end,
                logged_minutes,
                start_line,
                end_line,
                source_tz,
                " ".join(sorted(flags)),
                import_id,
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get(self, device_id: int, start_ts: int) -> LegacyOutage | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM legacy_outages WHERE device_id = ? AND start_ts = ?",
            (device_id, start_ts),
        ).fetchone()
        return _row_to_outage(row) if row is not None else None

    def between(self, device_id: int, start: int, end: int) -> list[LegacyOutage]:
        """Legacy entries overlapping ``[start, end]``: started at or
        before ``end``, and either still open or ended at or after
        ``start`` -- the same overlap rule `storage.outages.OutageStore.
        events`/`gaps` already use (DATA-02, qa-report-data-01.md: the
        range query reconciliation needs, which `get()`'s exact-start
        lookup alone could never provide)."""
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM legacy_outages WHERE device_id = ? AND start_ts <= ?"
            " AND (end_ts IS NULL OR end_ts >= ?) ORDER BY start_ts",
            (device_id, end, start),
        ).fetchall()
        return [_row_to_outage(row) for row in rows]


__all__ = ["LegacyOutage", "LegacyStore"]
