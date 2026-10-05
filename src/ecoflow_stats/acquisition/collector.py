"""Per-device, per-tick collection: fetch one device's quota, normalize it,
and store the result — or record exactly why not. Every failure is
isolated: one device's problem, or one unexpected bug, never stops another
device's collection on this tick or this device's collection on the next
one (acquisition requirements: per-tick failure isolation, fetch outcomes
are recorded).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ecoflow_stats.acquisition.ecoflow_client import (
    CloudApiError,
    CloudBadPayload,
    CloudError,
    CloudHttpError,
    CloudNetworkError,
    CloudTimeout,
)
from ecoflow_stats.storage.failures import FetchFailure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.registry import AdapterRegistry
    from ecoflow_stats.ports import Clock, DeviceCloud, FailureLog, SampleStore


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
) -> bool:
    """Fetch and store exactly one sample for one device, or record exactly
    one fetch failure. Returns whether a sample was stored."""
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
    return True


async def run_tick(
    devices: Sequence[CollectorDevice],
    *,
    cloud: DeviceCloud,
    registry: AdapterRegistry,
    samples: SampleStore,
    failures: FailureLog,
    clock: Clock,
) -> list[bool]:
    """Collect every configured device concurrently for one tick.

    ``collect_one`` already catches everything it expects; this wrapper is
    the final safety net so an unanticipated exception from one device's
    path can never prevent another device's result for this same tick.
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
            )
        except Exception:  # noqa: BLE001 — the final isolation boundary for this tick
            return False

    return list(await asyncio.gather(*(_isolated(device) for device in devices)))


__all__ = ["CollectorDevice", "collect_one", "run_tick"]
