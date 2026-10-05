"""The ``samples`` table: immutable raw truth, one row per device per
minute. Only ``add`` ever writes to it, and only as an insert — there is no
update or delete here by design (storage requirement: raw samples are
immutable once written)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ecoflow_stats.devices.reading import FIELD_NAMES, Reading

if TYPE_CHECKING:
    from ecoflow_stats.ports import Origin

_COLUMNS = ("device_id", "ts", "origin", *FIELD_NAMES)


@dataclass(frozen=True, slots=True)
class StoredSample:
    """One persisted ``samples`` row: envelope fields plus the normalized
    reading itself."""

    device_id: int
    ts: int
    origin: Origin
    reading: Reading


def _row_to_sample(row: sqlite3.Row) -> StoredSample:
    return StoredSample(
        device_id=row["device_id"],
        ts=row["ts"],
        origin=row["origin"],
        reading=Reading(**{name: row[name] for name in FIELD_NAMES}),
    )


class SampleStore:
    """Synchronous ``samples`` table access."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, device_id: int, ts: int, origin: Origin, reading: Reading) -> bool:
        """Insert one sample; return ``False`` if that device/minute
        already holds one. The app's own collected sample always wins over
        a later legacy import for the same minute, because that import
        also goes through this same no-op-on-conflict path."""
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        values = (device_id, ts, origin, *(getattr(reading, name) for name in FIELD_NAMES))
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO samples ({columns}) VALUES ({placeholders})", values
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def latest(self, device_id: int) -> StoredSample | None:
        """Return the most recently recorded sample for a device, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM samples WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        return _row_to_sample(row) if row is not None else None

    def between(self, device_id: int, start: int, end: int) -> Iterator[StoredSample]:
        """Yield a device's samples within ``[start, end]``, in timestamp order."""
        rows = self._conn.execute(
            "SELECT * FROM samples WHERE device_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (device_id, start, end),
        ).fetchall()
        return (_row_to_sample(row) for row in rows)


__all__ = ["SampleStore", "StoredSample"]
