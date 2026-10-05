"""The ``samples`` table: immutable raw truth, one row per device per
minute. Only ``add`` ever writes to it, and only as an insert — there is no
update or delete here by design (storage requirement: raw samples are
immutable once written)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ecoflow_stats.devices.reading import FIELD_NAMES, Reading

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from ecoflow_stats.ports import Origin

_COLUMNS = ("device_id", "ts", "origin", *FIELD_NAMES)
_DEFAULT_BATCH_SIZE = 5_000


@dataclass(frozen=True, slots=True)
class StoredSample:
    """One persisted ``samples`` row: envelope fields plus the normalized
    reading itself."""

    device_id: int
    ts: int
    origin: Origin
    reading: Reading


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Outcome of one :meth:`SampleStore.add_batch` call."""

    inserted: int
    skipped_existing: int
    skipped_overlap: int


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

    def add_batch(
        self,
        device_id: int,
        origin: Origin,
        rows: Iterable[tuple[int, Reading]],
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> BatchResult:
        """Insert many samples, committing every ``batch_size`` rows as its
        own transaction — a history import that crashes partway through
        keeps every already-completed batch, so a re-run only has to
        redo the rows after the last commit, not the whole import
        (history-import requirement: resuming an interrupted import does
        not double-count).

        Classifies every row that already occupies its ``(device_id, ts)``
        slot by which origin got there first: ``skipped_existing`` means
        the app's own collected sample already won that minute (never
        overwritten, per "One Sample Per Device Per Minute"); ``skipped_
        overlap`` means an earlier run of this same import already
        inserted it (idempotent re-run).
        """
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        inserted = 0
        skipped_existing = 0
        skipped_overlap = 0
        pending = 0
        for ts, reading in rows:
            existing = self._conn.execute(
                "SELECT origin FROM samples WHERE device_id = ? AND ts = ?", (device_id, ts)
            ).fetchone()
            if existing is not None:
                if existing["origin"] == 1:
                    skipped_existing += 1
                else:
                    skipped_overlap += 1
                continue
            values = (device_id, ts, origin, *(getattr(reading, name) for name in FIELD_NAMES))
            self._conn.execute(
                f"INSERT OR IGNORE INTO samples ({columns}) VALUES ({placeholders})", values
            )
            inserted += 1
            pending += 1
            if pending >= batch_size:
                self._conn.commit()
                pending = 0
        if pending:
            self._conn.commit()
        return BatchResult(
            inserted=inserted, skipped_existing=skipped_existing, skipped_overlap=skipped_overlap
        )


__all__ = ["BatchResult", "SampleStore", "StoredSample"]
