"""Verified, read-only snapshot of the legacy ecoflow-panel sources.

Both `samples.db` and `outages.log` are copied into one snapshot directory
before either is read for real, so they represent the same instant
(history-import requirement: "Import Reads a Consistent Read-Only
Snapshot"; amendment item 8 extends the design's samples-only snapshot to
cover the log too, since the legacy watcher keeps appending to it during
the parallel-run transition). The row transform that turns a verified
`samples.db` copy into schema-v1 rows is a later work unit; this module
only proves the source is safe to read.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ecoflow_stats.devices.reading import FIELD_NAMES

_V1_COLUMNS = frozenset({"ts", *FIELD_NAMES})
_MAX_QUICK_CHECK_RETRIES = 3
_QUICK_CHECK_RETRY_DELAY_S = 2.0


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


__all__ = ["Snapshot", "SnapshotError", "make_snapshot"]
