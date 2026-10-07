"""Tests for `live_status.service.get_status`: wires `device_status`
(pure) to real storage -- the trailing judge() window and the ongoing-
outage lookup, both read fresh from storage on every call rather than
depending on any in-memory live detector state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.live_status.service import get_status
from ecoflow_stats.outages.model import DetectorConfig, Event
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.samples import SampleStore

_CONFIG = DetectorConfig()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


class _Env:
    def __init__(self, db_path: Path) -> None:
        self.db = Database(db_path)
        self.device_id = (
            DeviceStore(self.db.writer)
            .upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1)
            .id
        )
        self.sample_store = SampleStore(self.db.writer)
        self.outage_store = OutageStore(self.db.writer)

    def close(self) -> None:
        self.db.close()


def test_get_status_reports_no_data_for_a_device_with_no_samples(tmp_path: Path) -> None:
    """Pass-1: querying storage incorrectly for a brand-new device (for
    example, crashing on an empty result) would break the live status
    page before a single sample ever arrives."""
    env = _Env(tmp_path / "ecoflow-stats.db")
    try:
        status = get_status(
            env.device_id,
            sample_store=env.sample_store,
            outage_store=env.outage_store,
            now=_NOW,
            stale_threshold_s=180,
            poll_interval_s=60,
            config=_CONFIG,
        )

        assert status.reading is None
        assert status.outage.ongoing is False
    finally:
        env.close()


def test_get_status_detects_a_stale_repeated_payload_using_real_stored_history(
    tmp_path: Path,
) -> None:
    """Pass-1: fetching the wrong samples (or none at all) for the
    judge() trailing window would mean a real cached/repeated payload
    is reported as present/absent through this API instead of unknown
    -- a live-status consumer could show stale data as current."""
    env = _Env(tmp_path / "ecoflow-stats.db")
    try:
        repeated = Reading(grid_v=120.0, soc=80)
        env.sample_store.add(env.device_id, _NOW_TS - 180, 1, repeated)
        env.sample_store.add(env.device_id, _NOW_TS - 120, 1, repeated)
        env.sample_store.add(env.device_id, _NOW_TS - 60, 1, repeated)

        status = get_status(
            env.device_id,
            sample_store=env.sample_store,
            outage_store=env.outage_store,
            now=_NOW,
            stale_threshold_s=180,
            poll_interval_s=60,
            config=_CONFIG,
        )

        assert status.grid == "unknown"
    finally:
        env.close()


def test_get_status_reports_an_ongoing_outage_from_the_real_outage_store(tmp_path: Path) -> None:
    """Pass-1: not consulting the real `outage_events` table would mean
    the live status page never shows "power has been out since HH:MM"
    even though the derive job already recorded exactly that."""
    env = _Env(tmp_path / "ecoflow-stats.db")
    try:
        env.sample_store.add(env.device_id, _NOW_TS - 10, 1, Reading(grid_v=0.0, soc=70))
        env.outage_store.replace_from(
            env.device_id,
            None,
            [Event(start_ts=_NOW_TS - 600, kind="outage", readings=2)],
            [],
        )

        status = get_status(
            env.device_id,
            sample_store=env.sample_store,
            outage_store=env.outage_store,
            now=_NOW,
            stale_threshold_s=180,
            poll_interval_s=60,
            config=_CONFIG,
        )

        assert status.outage.ongoing is True
        assert status.outage.since == _NOW_TS - 600
    finally:
        env.close()
