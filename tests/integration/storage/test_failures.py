"""Integration tests for :class:`FailureLog`, against a real SQLite file."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog, FetchFailure


def _log(tmp_path: Path) -> tuple[FailureLog, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    return FailureLog(db.writer), db, device.id


def test_recording_a_failure_and_reading_it_back(tmp_path: Path) -> None:
    log, db, device_id = _log(tmp_path)
    try:
        log.record(
            device_id,
            FetchFailure(ts=1_000, outcome="timeout", code=None, attempts=2, latency_ms=15_000),
        )
        failures = log.between(device_id, 0, 2_000)
        assert len(failures) == 1
        assert failures[0].outcome == "timeout"
        assert failures[0].attempts == 2
    finally:
        db.close()


def test_a_second_failure_for_the_same_minute_does_not_duplicate(tmp_path: Path) -> None:
    """Matches the samples table's own insert-or-ignore semantics for the
    same (device, minute) key — a duplicate-write retry must not multiply
    the recorded outage evidence."""
    log, db, device_id = _log(tmp_path)
    try:
        log.record(
            device_id,
            FetchFailure(ts=1_000, outcome="timeout", code=None, attempts=1, latency_ms=None),
        )
        log.record(
            device_id,
            FetchFailure(ts=1_000, outcome="network", code=None, attempts=9, latency_ms=None),
        )
        failures = log.between(device_id, 0, 2_000)
        assert len(failures) == 1
        assert failures[0].outcome == "timeout"
    finally:
        db.close()


def test_between_excludes_failures_outside_the_range(tmp_path: Path) -> None:
    log, db, device_id = _log(tmp_path)
    try:
        for ts, outcome in ((50, "timeout"), (100, "network"), (200, "http")):
            log.record(
                device_id,
                FetchFailure(ts=ts, outcome=outcome, code=None, attempts=1, latency_ms=None),
            )
        failures = log.between(device_id, 60, 150)
        assert [f.ts for f in failures] == [100]
    finally:
        db.close()
