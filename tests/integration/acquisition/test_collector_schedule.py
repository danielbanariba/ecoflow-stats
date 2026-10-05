"""Integration tests for aligned tick scheduling.

Covers acquisition "Per-Device Collection Cadence Without Overlapping
Ticks": ticks land on a configured wall-clock-aligned cadence, and an
overrun skips ahead to the next future slot rather than firing immediately.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.acquisition.collector import CollectorDevice, next_aligned_slot, run_forever
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.samples import SampleStore
from tests.fakes import FakeDeviceCloud

_VALID_PAYLOAD = {"bmsMaster.soc": 77, "inv.acInVol": 115_000, "productName": "DELTA Pro"}


class _CountingClock:
    """Minimal Clock double: ``now()`` reads a settable instant that
    advances exactly when ``sleep_until`` is called with it, and
    ``sleep_until`` raises ``CancelledError`` after a fixed number of
    calls — a deterministic way to bound an otherwise-infinite loop."""

    def __init__(self, start: datetime, stop_after: int) -> None:
        self._now = start
        self._stop_after = stop_after
        self.sleep_calls = 0

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, when: datetime) -> None:
        self.sleep_calls += 1
        if self.sleep_calls > self._stop_after:
            raise asyncio.CancelledError
        self._now = when


def test_next_aligned_slot_returns_the_offset_second_of_the_current_interval() -> None:
    now = datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    slot = next_aligned_slot(now, interval_s=60, offset_s=30)
    assert slot == datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)


def test_next_aligned_slot_skips_to_the_following_slot_once_the_offset_has_passed() -> None:
    now = datetime(2026, 1, 1, 12, 0, 45, tzinfo=UTC)
    slot = next_aligned_slot(now, interval_s=60, offset_s=30)
    assert slot == datetime(2026, 1, 1, 12, 1, 30, tzinfo=UTC)


def test_next_aligned_slot_is_never_equal_to_now_exactly_on_a_boundary() -> None:
    """An overrun that finishes exactly on a slot must skip to the next
    future one, not fire again immediately for the same instant."""
    now = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)
    slot = next_aligned_slot(now, interval_s=60, offset_s=30)
    assert slot == datetime(2026, 1, 1, 12, 1, 30, tzinfo=UTC)


@pytest.mark.anyio
async def test_run_forever_ticks_repeatedly_on_the_configured_cadence(tmp_path: Path) -> None:
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        device = DeviceStore(db.writer).upsert(
            sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1
        )
        samples = SampleStore(db.writer)
        failures = FailureLog(db.writer)
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _VALID_PAYLOAD})
        clock = _CountingClock(datetime(2026, 1, 1, tzinfo=UTC), stop_after=3)

        with pytest.raises(asyncio.CancelledError):
            await run_forever(
                [CollectorDevice(device_id=device.id, sn="BA31ZEB1SF7F0001")],
                cloud=cloud,
                registry=AdapterRegistry(REGISTERED),
                samples=samples,
                failures=failures,
                clock=clock,
                poll_interval_s=60,
                poll_offset_s=30,
            )

        stored = sorted(s.ts for s in samples.between(device.id, 0, 10**12))
        assert len(stored) == 3
        assert stored[1] - stored[0] == 60
        assert stored[2] - stored[1] == 60
    finally:
        db.close()


@pytest.mark.anyio
async def test_run_forever_does_not_start_a_second_tick_before_the_first_finishes(
    tmp_path: Path,
) -> None:
    """A slow tick must not overlap the next one — the scheduler awaits
    each tick fully before computing the following slot."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        device = DeviceStore(db.writer).upsert(
            sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1
        )
        samples = SampleStore(db.writer)
        failures = FailureLog(db.writer)
        in_flight = 0
        max_concurrent = 0

        class _SlowCloud:
            async def list_devices(self) -> list[object]:
                return []

            async def fetch_quota(self, sn: str) -> dict[str, object]:
                nonlocal in_flight, max_concurrent
                in_flight += 1
                max_concurrent = max(max_concurrent, in_flight)
                await asyncio.sleep(0)
                in_flight -= 1
                return dict(_VALID_PAYLOAD)

        clock = _CountingClock(datetime(2026, 1, 1, tzinfo=UTC), stop_after=2)

        with pytest.raises(asyncio.CancelledError):
            await run_forever(
                [CollectorDevice(device_id=device.id, sn="BA31ZEB1SF7F0001")],
                cloud=_SlowCloud(),
                registry=AdapterRegistry(REGISTERED),
                samples=samples,
                failures=failures,
                clock=clock,
                poll_interval_s=60,
                poll_offset_s=30,
            )

        assert max_concurrent == 1
    finally:
        db.close()
