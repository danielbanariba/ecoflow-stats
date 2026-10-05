"""Integration tests for :class:`SampleStore`, against a real SQLite file."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.samples import SampleStore


def _store(tmp_path: Path) -> tuple[SampleStore, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    return SampleStore(db.writer), db, device.id


def test_adding_a_sample_stores_it_and_latest_returns_it(tmp_path: Path) -> None:
    store, db, device_id = _store(tmp_path)
    try:
        reading = Reading(soc=80, grid_v=120.5, cycles=10)
        added = store.add(device_id, 1_000, 1, reading)
        assert added is True
        latest = store.latest(device_id)
        assert latest is not None
        assert latest.ts == 1_000
        assert latest.origin == 1
        assert latest.reading == reading
    finally:
        db.close()


def test_missing_fields_round_trip_as_none_not_zero(tmp_path: Path) -> None:
    """Named Defect 'missing read as zero': a field the adapter could not
    map must still be NULL after a full store-and-reload round trip."""
    store, db, device_id = _store(tmp_path)
    try:
        reading = Reading(soc=None, grid_v=None, cycles=None)
        store.add(device_id, 1_000, 1, reading)
        latest = store.latest(device_id)
        assert latest is not None
        assert latest.reading.soc is None
        assert latest.reading.grid_v is None
        assert latest.reading.cycles is None
    finally:
        db.close()


def test_a_second_sample_for_the_same_minute_does_not_overwrite_the_first(
    tmp_path: Path,
) -> None:
    """Raw samples are immutable once written (storage spec): a later write
    for the same device/minute — for example a legacy import racing a live
    collection — must be a no-op, not an overwrite."""
    store, db, device_id = _store(tmp_path)
    try:
        first = Reading(soc=50)
        second = Reading(soc=99)
        added_first = store.add(device_id, 1_000, 1, first)
        added_second = store.add(device_id, 1_000, 2, second)
        assert added_first is True
        assert added_second is False
        latest = store.latest(device_id)
        assert latest is not None
        assert latest.reading.soc == 50
        assert latest.origin == 1
    finally:
        db.close()


def test_latest_returns_none_when_no_samples_exist(tmp_path: Path) -> None:
    store, db, device_id = _store(tmp_path)
    try:
        assert store.latest(device_id) is None
    finally:
        db.close()


def test_between_returns_only_samples_in_the_inclusive_range_in_order(
    tmp_path: Path,
) -> None:
    store, db, device_id = _store(tmp_path)
    try:
        for ts in (50, 100, 150, 200):
            store.add(device_id, ts, 1, Reading(soc=ts // 10))
        result = list(store.between(device_id, 100, 150))
        assert [s.ts for s in result] == [100, 150]
    finally:
        db.close()
