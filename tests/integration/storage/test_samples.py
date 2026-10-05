"""Integration tests for :class:`SampleStore`, against a real SQLite file."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

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


def test_add_batch_inserts_every_row_once(tmp_path: Path) -> None:
    store, db, device_id = _store(tmp_path)
    try:
        rows = [(1_000, Reading(soc=90)), (1_060, Reading(soc=89)), (1_120, Reading(soc=88))]
        result = store.add_batch(device_id, 2, rows)
        assert result.inserted == 3
        assert result.skipped_existing == 0
        assert result.skipped_overlap == 0
        assert [s.ts for s in store.between(device_id, 0, 2_000)] == [1_000, 1_060, 1_120]
        assert all(s.origin == 2 for s in store.between(device_id, 0, 2_000))
    finally:
        db.close()


def test_add_batch_skips_a_minute_already_held_by_the_collector(tmp_path: Path) -> None:
    """One Sample Per Device Per Minute — the App's Own Sample Wins: an
    imported row must never overwrite a minute the live collector already
    recorded, and the report must say so distinctly from an ordinary
    re-import overlap."""
    store, db, device_id = _store(tmp_path)
    try:
        store.add(device_id, 1_000, 1, Reading(soc=50))
        result = store.add_batch(device_id, 2, [(1_000, Reading(soc=99))])
        assert result.inserted == 0
        assert result.skipped_existing == 1
        assert result.skipped_overlap == 0
        latest = store.latest(device_id)
        assert latest is not None
        assert latest.reading.soc == 50
        assert latest.origin == 1
    finally:
        db.close()


def test_add_batch_skips_a_minute_already_imported_by_an_earlier_run(tmp_path: Path) -> None:
    """History-import idempotency: re-running the import against
    unchanged sources must not duplicate or re-count a row an earlier run
    already inserted."""
    store, db, device_id = _store(tmp_path)
    try:
        first_run = store.add_batch(device_id, 2, [(1_000, Reading(soc=50))])
        second_run = store.add_batch(device_id, 2, [(1_000, Reading(soc=50))])
        assert first_run.inserted == 1
        assert second_run.inserted == 0
        assert second_run.skipped_overlap == 1
        assert second_run.skipped_existing == 0
    finally:
        db.close()


def test_add_batch_commits_completed_batches_so_a_crash_mid_batch_is_resumable(
    tmp_path: Path,
) -> None:
    """Batches are committed as they complete, not all at the very end —
    an import that crashes partway through still keeps every row from a
    batch that finished, so a re-run only redoes the rows after the last
    commit, not the whole import."""
    store, db, device_id = _store(tmp_path)
    try:

        def rows() -> Iterator[tuple[int, Reading]]:
            yield 1_000, Reading(soc=1)
            yield 1_060, Reading(soc=2)
            yield 1_120, Reading(soc=3)
            raise RuntimeError("simulated crash mid-batch")

        with pytest.raises(RuntimeError, match="simulated crash"):
            store.add_batch(device_id, 2, rows(), batch_size=2)

        # A fresh connection proves the first full batch (2 rows) was
        # durably committed, independent of this test's own connection.
        fresh = sqlite3.connect(db.path)
        try:
            (count,) = fresh.execute(
                "SELECT COUNT(*) FROM samples WHERE device_id = ?", (device_id,)
            ).fetchone()
        finally:
            fresh.close()
        assert count == 2
    finally:
        db.close()
