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
from ecoflow_stats.notifications.service import NotificationService
from ecoflow_stats.outages.detector import LiveOutageState, OutageMachine
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.notifications import NotificationLedger
from ecoflow_stats.storage.samples import SampleStore
from tests.fakes import FakeClock, FakeDeviceCloud, RecordingNotifier

_VALID_PAYLOAD = {"bmsMaster.soc": 77, "inv.acInVol": 115_000, "productName": "DELTA Pro"}
_BELOW_THRESHOLD_PAYLOAD = {"bmsMaster.soc": 77, "inv.acInVol": 0, "productName": "DELTA Pro"}


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
async def test_a_successful_fetch_marks_the_outage_derivation_dirty(tmp_path: Path) -> None:
    """Without this, a live-collected sample never triggers a recompute:
    the 5-minute derive job only ever sees a device as dirty when an
    import marks it (history_import.service.run_import already does this
    for its own path) -- a device fed only by the collector would never
    get its outages/gaps derived at all (batch 7's documented known gap).
    """
    db, samples, failures, device_id = _env(tmp_path)
    try:
        derivations = DerivationStore(db.writer)
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
            derivation_store=derivations,
        )

        assert stored is True
        derivation = derivations.get(device_id, "outages")
        assert derivation is not None
        assert derivation.dirty_from_ts == int(clock.now().timestamp())
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_successful_fetch_marks_the_rollups_derivation_dirty(tmp_path: Path) -> None:
    """Without this, a live-collected sample never triggers a rollups
    recompute either: `rollups.service.derive_rollups` only recomputes a
    device's `daily_rollups` when its "rollups" derivation is dirty, and
    nothing else ever marks it -- a device fed only by the collector
    would never get a single day of `daily_rollups` populated.
    """
    db, samples, failures, device_id = _env(tmp_path)
    try:
        derivations = DerivationStore(db.writer)
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
            derivation_store=derivations,
        )

        assert stored is True
        derivation = derivations.get(device_id, "rollups")
        assert derivation is not None
        assert derivation.dirty_from_ts == int(clock.now().timestamp())
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_failed_fetch_does_not_mark_the_outage_derivation_dirty(tmp_path: Path) -> None:
    """A fetch that stored nothing has nothing new for the derive job to
    recompute -- marking dirty anyway would force a needless recompute on
    every failed tick."""
    db, samples, failures, device_id = _env(tmp_path)
    try:
        derivations = DerivationStore(db.writer)
        cloud = FakeDeviceCloud(
            quota_results={"BA31ZEB1SF7F0001": CloudTimeout("connection timed out")}
        )
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
            derivation_store=derivations,
        )

        assert derivations.get(device_id, "outages") is None
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


class _RaisingNotificationService:
    """A `NotificationService`-shaped double whose `handle_transition`
    always raises -- unlike the real service (which already isolates a
    `NotifyError` internally), this proves `collect_one` itself is the
    final backstop against ANY unexpected notification-path failure."""

    async def handle_transition(self, device_id: int, transition: object) -> None:
        raise RuntimeError("boom")


@pytest.mark.anyio
async def test_a_single_below_threshold_reading_triggers_a_live_start_notification(
    tmp_path: Path,
) -> None:
    """Notifications requirement "Alert on the First Below-Threshold
    Reading": a single below-floor tick must notify immediately, before
    any 2-reading debounce confirms an official outage."""
    db, samples, failures, device_id = _env(tmp_path)
    try:
        ledger = NotificationLedger(db.writer)
        notifier = RecordingNotifier()
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        service = NotificationService(ledger=ledger, notifier=notifier, clock=clock, lang="en")
        live_state = LiveOutageState(machine=OutageMachine())
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _BELOW_THRESHOLD_PAYLOAD})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")

        await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
            live_state=live_state,
            notification_service=service,
            detector_config=DetectorConfig(),
        )

        assert len(notifier.sent) == 1
        assert notifier.sent[0].title == "Power is out"
    finally:
        db.close()


@pytest.mark.anyio
async def test_the_live_state_persists_across_ticks_so_a_second_reading_does_not_resend(
    tmp_path: Path,
) -> None:
    """Pass-1: if `collect_one` built a fresh `LiveOutageState` on every
    call instead of mutating the one it is given, every single tick
    would look like a brand-new outage start, re-sending the start
    alert on every below-threshold reading instead of exactly once."""
    db, samples, failures, device_id = _env(tmp_path)
    try:
        ledger = NotificationLedger(db.writer)
        notifier = RecordingNotifier()
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        service = NotificationService(ledger=ledger, notifier=notifier, clock=clock, lang="en")
        live_state = LiveOutageState(machine=OutageMachine())
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _BELOW_THRESHOLD_PAYLOAD})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")

        for _ in range(2):
            await collect_one(
                device,
                cloud=cloud,
                registry=AdapterRegistry(REGISTERED),
                samples=samples,
                failures=failures,
                clock=clock,
                live_state=live_state,
                notification_service=service,
                detector_config=DetectorConfig(),
            )

        # The second tick only confirms the outage; it doesn't start a new one.
        assert len(notifier.sent) == 1
        assert live_state.machine.mode == "absent"
    finally:
        db.close()


@pytest.mark.anyio
async def test_an_unexpected_notification_failure_does_not_stop_the_sample_from_being_stored(
    tmp_path: Path,
) -> None:
    """Notifications requirement "Notification Failures Are Isolated
    From Collection": collection must succeed even when the entire
    notification path blows up unexpectedly, not just on a handled
    `NotifyError`."""
    db, samples, failures, device_id = _env(tmp_path)
    try:
        live_state = LiveOutageState(machine=OutageMachine())
        cloud = FakeDeviceCloud(quota_results={"BA31ZEB1SF7F0001": _BELOW_THRESHOLD_PAYLOAD})
        device = CollectorDevice(device_id=device_id, sn="BA31ZEB1SF7F0001")
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

        stored = await collect_one(
            device,
            cloud=cloud,
            registry=AdapterRegistry(REGISTERED),
            samples=samples,
            failures=failures,
            clock=clock,
            live_state=live_state,
            notification_service=_RaisingNotificationService(),
            detector_config=DetectorConfig(),
        )

        assert stored is True
        assert samples.latest(device_id) is not None
    finally:
        db.close()
