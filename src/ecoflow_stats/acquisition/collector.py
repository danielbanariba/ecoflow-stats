"""Per-device, per-tick collection: fetch one device's quota, normalize it,
and store the result — or record exactly why not. Every failure is
isolated: one device's problem, or one unexpected bug, never stops another
device's collection on this tick or this device's collection on the next
one (acquisition requirements: per-tick failure isolation, fetch outcomes
are recorded).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ecoflow_stats.acquisition.ecoflow_client import (
    CloudApiError,
    CloudBadPayload,
    CloudError,
    CloudHttpError,
    CloudNetworkError,
    CloudTimeout,
)
from ecoflow_stats.outages.model import DetectorConfig, Ended, Started
from ecoflow_stats.storage.failures import FetchFailure

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ecoflow_stats.devices.registry import AdapterRegistry
    from ecoflow_stats.notifications.service import NotificationService
    from ecoflow_stats.outages.detector import LiveOutageState
    from ecoflow_stats.ports import Clock, DerivationStore, DeviceCloud, FailureLog, SampleStore

logger = logging.getLogger(__name__)
_DEFAULT_DETECTOR_CONFIG = DetectorConfig()


@dataclass(frozen=True, slots=True)
class CollectorDevice:
    """One device's identity for the collector: which row to attribute
    samples and failures to, which serial to fetch, and any operator-pinned
    adapter override (``ECOFLOW_DEVICES=SN:adapter_id``)."""

    device_id: int
    sn: str
    configured_adapter_id: str | None = None


def _categorize(exc: Exception) -> tuple[str, str | None]:
    """Map a cloud-client exception onto a ``fetch_failures.outcome`` plus
    an optional code, so the stored reason matches what actually happened."""
    if isinstance(exc, CloudTimeout):
        return "timeout", None
    if isinstance(exc, CloudNetworkError):
        return "network", None
    if isinstance(exc, CloudHttpError):
        return "http", str(exc.status)
    if isinstance(exc, CloudApiError):
        return "api", exc.code
    if isinstance(exc, CloudBadPayload):
        return "bad_payload", None
    return "internal", None


async def collect_one(
    device: CollectorDevice,
    *,
    cloud: DeviceCloud,
    registry: AdapterRegistry,
    samples: SampleStore,
    failures: FailureLog,
    clock: Clock,
    derivation_store: DerivationStore | None = None,
    live_state: LiveOutageState | None = None,
    notification_service: NotificationService | None = None,
    detector_config: DetectorConfig = _DEFAULT_DETECTOR_CONFIG,
) -> bool:
    """Fetch and store exactly one sample for one device, or record exactly
    one fetch failure. Returns whether a sample was stored.

    ``derivation_store`` is optional only so every existing caller and
    test that predates this wiring keeps working unchanged; the real
    collector path (``run_forever``, via ``bootstrap.build``) always
    supplies it now. When given, and a sample was actually stored, marks
    the device's ``outages`` derivation dirty from this tick's timestamp
    -- the same trigger `history_import.service.run_import` already uses
    for its own path (design-data section 4.3: "collector tick
    (dirty_from = sample ts)").

    ``live_state``/``notification_service`` are optional for the same
    backward-compatibility reason; the real path supplies both only when
    `ECOFLOW_STATS_NTFY_TOPIC` is configured (notifications requirement:
    "Notifications Are Opt-In"). When given, feeds this tick's reading
    through the device's own live detector state and forwards any
    `Started`/`Ended` transition to the notification service -- isolated
    the same way a cloud or storage failure already is, so an unexpected
    notification-path failure can never stop this or the next tick
    (notifications requirement: "Notification Failures Are Isolated From
    Collection").
    """
    ts = int(clock.now().timestamp())
    try:
        payload = await cloud.fetch_quota(device.sn)
    except CloudError as exc:
        outcome, code = _categorize(exc)
        failures.record(
            device.device_id,
            FetchFailure(ts=ts, outcome=outcome, code=code, attempts=1, latency_ms=None),
        )
        return False

    try:
        adapter = registry.resolve(
            explicit_adapter_id=device.configured_adapter_id, info=None, payload=payload
        )
        normalized = adapter.normalize(payload)
    except Exception:  # noqa: BLE001 — isolation boundary, see run_tick's docstring
        # Not a cloud failure — an unclaimed payload (no catch-all adapter
        # registered) or any other bug in our own mapping code. Isolated
        # the same as a cloud failure: recorded, never raised further.
        failures.record(
            device.device_id,
            FetchFailure(ts=ts, outcome="internal", code=None, attempts=1, latency_ms=None),
        )
        return False

    try:
        samples.add(device.device_id, ts, 1, normalized.reading)
    except sqlite3.Error:
        failures.record(
            device.device_id,
            FetchFailure(ts=ts, outcome="store", code=None, attempts=1, latency_ms=None),
        )
        return False
    if derivation_store is not None:
        derivation_store.mark_dirty(device.device_id, "outages", ts)
    if live_state is not None:
        transitions = live_state.feed(ts, normalized.reading, config=detector_config)
        if notification_service is not None:
            for transition in transitions:
                if isinstance(transition, (Started, Ended)):
                    try:
                        await notification_service.handle_transition(device.device_id, transition)
                    except Exception:  # notification failures must never touch collection
                        logger.exception(
                            "notification handling failed for device id %s", device.device_id
                        )
    return True


async def run_tick(
    devices: Sequence[CollectorDevice],
    *,
    cloud: DeviceCloud,
    registry: AdapterRegistry,
    samples: SampleStore,
    failures: FailureLog,
    clock: Clock,
    derivation_store: DerivationStore | None = None,
    live_states: Mapping[int, LiveOutageState] | None = None,
    notification_service: NotificationService | None = None,
    detector_config: DetectorConfig = _DEFAULT_DETECTOR_CONFIG,
) -> list[bool]:
    """Collect every configured device concurrently for one tick.

    ``collect_one`` already catches everything it expects; this wrapper is
    the final safety net so an unanticipated exception from one device's
    path can never prevent another device's result for this same tick.

    When ``notification_service`` is given, also retries anything the
    notification ledger still has pending once per tick (design D10:
    "retried each tick with backoff for up to 6 hours, then expired"),
    isolated the same way a per-device failure already is.
    """

    async def _isolated(device: CollectorDevice) -> bool:
        try:
            return await collect_one(
                device,
                cloud=cloud,
                registry=registry,
                samples=samples,
                failures=failures,
                clock=clock,
                derivation_store=derivation_store,
                live_state=live_states.get(device.device_id) if live_states else None,
                notification_service=notification_service,
                detector_config=detector_config,
            )
        except Exception:  # noqa: BLE001 — the final isolation boundary for this tick
            return False

    results = list(await asyncio.gather(*(_isolated(device) for device in devices)))
    if notification_service is not None:
        try:
            await notification_service.retry_due(clock.now())
        except Exception:  # a retry failure must never break ticking
            logger.exception("notification retry pass failed")
    return results


def next_aligned_slot(now: datetime, *, interval_s: int, offset_s: int) -> datetime:
    """The next wall-clock instant, strictly after ``now``, that lands
    ``offset_s`` seconds into an ``interval_s``-aligned slot of the epoch.

    Always strictly in the future: an overrun that finishes exactly on a
    slot boundary skips ahead to the next one rather than firing again
    immediately for the same instant.
    """
    now_epoch = int(now.timestamp())
    k = (now_epoch - offset_s) // interval_s + 1
    candidate_epoch = k * interval_s + offset_s
    return datetime.fromtimestamp(candidate_epoch, tz=UTC)


async def run_forever(
    devices: Sequence[CollectorDevice],
    *,
    cloud: DeviceCloud,
    registry: AdapterRegistry,
    samples: SampleStore,
    failures: FailureLog,
    clock: Clock,
    poll_interval_s: int = 60,
    poll_offset_s: int = 30,
    derivation_store: DerivationStore | None = None,
    live_states: Mapping[int, LiveOutageState] | None = None,
    notification_service: NotificationService | None = None,
    detector_config: DetectorConfig = _DEFAULT_DETECTOR_CONFIG,
) -> None:
    """Run one tick per aligned slot, forever, until cancelled.

    Each slot is awaited in full before the next one is computed, so a
    slow tick is never overlapped by the next (acquisition: per-device
    collection cadence without overlapping ticks). An overrun — a tick
    that finishes after its next scheduled slot has already passed —
    skips straight to the following future slot rather than firing
    immediately or trying to catch up on missed ones.
    """
    while True:
        next_at = next_aligned_slot(clock.now(), interval_s=poll_interval_s, offset_s=poll_offset_s)
        await clock.sleep_until(next_at)
        await run_tick(
            devices,
            cloud=cloud,
            registry=registry,
            samples=samples,
            failures=failures,
            clock=clock,
            derivation_store=derivation_store,
            live_states=live_states,
            notification_service=notification_service,
            detector_config=detector_config,
        )


__all__ = ["CollectorDevice", "collect_one", "next_aligned_slot", "run_forever", "run_tick"]
