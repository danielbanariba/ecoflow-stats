"""Verified, read-only snapshot of the legacy ecoflow-panel sources, and
the row-by-row transform of its `samples.db` into schema-v1 samples.

Both `samples.db` and `outages.log` are copied into one snapshot directory
before either is read for real, so they represent the same instant
(history-import requirement: "Import Reads a Consistent Read-Only
Snapshot"; amendment item 8 extends the design's samples-only snapshot to
cover the log too, since the legacy watcher keeps appending to it during
the parallel-run transition). The legacy `samples` table already shares
all 21 column names with this app's own schema, so the transform is a
straight copy with validation — no field renaming, unlike a live-device
adapter.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ecoflow_stats.devices.reading import FIELD_NAMES, Reading

_V1_COLUMNS = frozenset({"ts", *FIELD_NAMES})
_MAX_QUICK_CHECK_RETRIES = 3
_QUICK_CHECK_RETRY_DELAY_S = 2.0
_MIN_VALID_TS = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())
"""Legacy history cannot predate the panel itself; a timestamp before this
is corrupt data, not a real sample (history-import requirement: a row
outside the valid range is rejected, not imported)."""


class SnapshotError(Exception):
    """The legacy sources could not be safely snapshotted or verified."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Paths to one verified, read-only copy of the legacy sources."""

    samples_db: Path
    outage_log: Path


def _copy_samples_db(source: Path, dest_dir: Path) -> Path:
    """Copy `samples.db` and its `-wal` sidecar, when present — a WAL
    database is not safely readable without it, since not-yet-checkpointed
    rows live only in the sidecar."""
    dest = dest_dir / source.name
    shutil.copy2(source, dest)
    wal_source = source.with_name(source.name + "-wal")
    if wal_source.exists():
        shutil.copy2(wal_source, dest_dir / wal_source.name)
    return dest


def _quick_check(
    path: Path,
    *,
    max_retries: int = _MAX_QUICK_CHECK_RETRIES,
    retry_delay_s: float = _QUICK_CHECK_RETRY_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run `PRAGMA quick_check` against the copy, retrying up to
    `max_retries` times, `retry_delay_s` apart, before giving up."""
    last_result: str | None = None
    for attempt in range(max_retries + 1):
        try:
            conn = sqlite3.connect(path)
            try:
                (last_result,) = conn.execute("PRAGMA quick_check").fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            last_result = str(exc)
        else:
            if last_result == "ok":
                return
        if attempt < max_retries:
            sleep(retry_delay_s)
    raise SnapshotError(
        f"{path}: failed PRAGMA quick_check after {max_retries + 1} attempts: {last_result}"
    )


def _verify_schema(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        (user_version,) = conn.execute("PRAGMA user_version").fetchone()
        if user_version != 1:
            raise SnapshotError(f"{path}: expected schema v1, found user_version={user_version}")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
        missing = _V1_COLUMNS - columns
        if missing:
            raise SnapshotError(f"{path}: samples table is missing columns: {sorted(missing)}")
    finally:
        conn.close()


def make_snapshot(
    *,
    samples_db: Path,
    outage_log: Path,
    snapshot_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> Snapshot:
    """Copy both legacy sources into `snapshot_dir`, verify the
    `samples.db` copy, and return paths to the verified copies.

    Neither original source is ever opened for anything but the plain
    file copy itself — every later read goes through the returned
    snapshot, never back to the live files.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    samples_copy = _copy_samples_db(samples_db, snapshot_dir)
    outage_log_copy = snapshot_dir / outage_log.name
    shutil.copy2(outage_log, outage_log_copy)

    _quick_check(samples_copy, sleep=sleep)
    _verify_schema(samples_copy)

    return Snapshot(samples_db=samples_copy, outage_log=outage_log_copy)


@dataclass(frozen=True, slots=True)
class TransformedRow:
    """One legacy row, validated and mapped onto schema v1."""

    ts: int
    reading: Reading


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """One legacy row that could not be trusted, with why."""

    ts: object
    reason: str


def transform_row(raw: Mapping[str, object], *, now: datetime) -> TransformedRow | RejectedRow:
    """Map one legacy `samples.db` row onto schema v1, or reject it.

    `ts` must be a real integer within ``[2020-01-01, now + 1 day]``, and
    every measurement value must be numeric or ``NULL`` — anything else
    (including a `bool`, which is an `int` subclass) is rejected rather
    than silently coerced, the same discipline `devices.adapter.num()`
    applies to a live payload.
    """
    ts_raw = raw.get("ts")
    if not isinstance(ts_raw, int) or isinstance(ts_raw, bool):
        return RejectedRow(ts=ts_raw, reason=f"ts is not an integer: {ts_raw!r}")
    max_valid_ts = int((now + timedelta(days=1)).timestamp())
    if not (_MIN_VALID_TS <= ts_raw <= max_valid_ts):
        return RejectedRow(ts=ts_raw, reason=f"ts {ts_raw} is outside the valid import range")

    values: dict[str, object] = {}
    for name in FIELD_NAMES:
        value = raw.get(name)
        if isinstance(value, bool) or (value is not None and not isinstance(value, (int, float))):
            return RejectedRow(ts=ts_raw, reason=f"{name} is not numeric or NULL: {value!r}")
        values[name] = value

    return TransformedRow(ts=ts_raw, reading=Reading(**values))


def iter_legacy_rows(conn: sqlite3.Connection) -> Iterator[Mapping[str, object]]:
    """Read every row of a verified `samples.db` copy's `samples` table,
    in `ts` order. `conn` must have a mapping-like `row_factory`
    (``sqlite3.Row``) set by the caller."""
    for row in conn.execute("SELECT * FROM samples ORDER BY ts"):
        yield dict(row)


__all__ = [
    "RejectedRow",
    "Snapshot",
    "SnapshotError",
    "TransformedRow",
    "iter_legacy_rows",
    "make_snapshot",
    "transform_row",
]
