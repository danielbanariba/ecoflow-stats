"""Integration tests for per-device, per-tick collection and isolation.

Covers acquisition "Per-Tick Failure Isolation" and "Fetch Outcomes Are
Recorded", against real SQLite storage and a scripted fake cloud — no
real network call is ever made.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.acquisition.collector import CollectorDevice, collect_one, run_tick
from ecoflow_stats.acquisition.ecoflow_client import CloudHttpError, CloudNetworkError, CloudTimeout
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.samples import SampleStore
from tests.fakes import FakeClock, FakeDeviceCloud

_VALID_PAYLOAD = {"bmsMaster.soc": 77, "inv.acInVol": 115_000, "productName": "DELTA Pro"}


def _env(tmp_path: Path) -> tuple[Database, SampleStore, FailureLog, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1
    )
    return db, SampleStore(db.writer), FailureLog(db.writer), device.id


@pytest.mark.anyio
async def test_a_successful_fetch_stores_a_normalized_sample(tmp_path: Path) -> None:
    db, samples, failures, device_id = _env(tmp_path)
    try:
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _VALID_PAYLOAD})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        stored = await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
        )

        assert stored is True
        latest = samples.latest(device_id)
        assert latest is not None
        assert latest.reading.soc == 77
        assert failures.between(device_id, 0, 10**12) == []
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_cloud_timeout_records_a_failure_and_stores_no_sample(tmp_path: Path) -> None:
    db, samples, failures, device_id = _env(tmp_path)
    try:
        cloud = FakeDeviceCloud(
            quota_results={"BA31ZEB1SF7F0001": CloudTimeout("connection timed out")}
        )
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        stored = await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
        )

        assert stored is False
        assert samples.latest(device_id) is None
        recorded = failures.between(device_id, 0, 10**12)
        assert len(recorded) == 1
        assert recorded[0].outcome == "timeout"
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_cloud_http_error_records_the_status_code(tmp_path: Path) -> None:
    db, samples, failures, device_id = _env(tmp_path)
    try:
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": CloudHttpError(503)})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
        )

        recorded = failures.between(device_id, 0, 10**12)
        assert recorded[0].outcome == "http"
        assert recorded[0].code == "503"
    finally:
        db.close()


@pytest.mark.anyio
async def test_an_unclaimed_payload_is_recorded_as_internal_not_a_crash(tmp_path: Path) -> None:
    """A registry with no catch-all adapter raises LookupError on resolve —
    collect_one must isolate that as a recorded failure, not let it crash
    the caller (acquisition: per-tick failure isolation applies to our own
    bugs too, not only cloud-side ones)."""
    db, samples, failures, device_id = _env(tmp_path)
    try:
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _VALID_PAYLOAD})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        stored = await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry([]),  # no catch-all registered
            samples=samples,
            failures=failures,
            clock=clock,
        )

        assert stored is False
        recorded = failures.between(device_id, 0, 10**12)
        assert recorded[0].outcome == "internal"
    finally:
        db.close()


@pytest.mark.anyio
async def test_one_devices_failure_does_not_block_another_devices_collection(
    tmp_path: Path,
) -> None:
    """Acquisition: 'One device's failure does not block another device's
    collection' — run_tick must still store device B's sample even though
    device A's fetch failed."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        device_store = DeviceStore(db.writer)
        a = device_store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1)
        b = device_store.upsert(sn="BA31ZEB1SF7F0002", adapter_id="generic", created_at=1)
        samples = SampleStore(db.writer)
        failures = FailureLog(db.writer)
        cloud = FakeDeviceCloud(
            quota_results={
                "BA31ZEB1SF7F0001": CloudNetworkError("name resolution failed"),
                "BA31ZEB1SF7F0002": _VALID_PAYLOAD,
            }
        )
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        results = await run_tick(
            [
                CollectorDevice(device_id=a.id, sn="BA31ZEB1SF7F0001"),
                CollectorDevice(device_id=b.id, sn="BA31ZEB1SF7F0002"),
            ],
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
        )

        assert results == [False, True]
        assert samples.latest(a.id) is None
        assert samples.latest(b.id) is not None
        assert len(failures.between(a.id, 0, 10**12)) == 1
    finally:
        db.close()
