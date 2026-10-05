"""Test doubles shared across the suite.

``RecordingNotifier`` is added once the notification work unit needs it.
Triangulation skipped for the fakes themselves: they are test
infrastructure, not behavior under test, and are exercised indirectly
through the real tests that use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo


class FakeClock:
    """Deterministic clock: ``now()`` reads a settable instant, and
    ``sleep_until`` jumps straight to the requested time instead of
    actually waiting, so a test never races the real wall clock."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, when: datetime) -> None:
        self._now = max(self._now, when)


@dataclass
class FakeDeviceCloud:
    """Scriptable ``DeviceCloud`` double.

    ``quota_results`` maps a serial to either a raw payload (returned) or
    an ``Exception`` instance (raised) — so a test can script one device's
    failure without touching another's success. Every call is recorded.
    """

    devices: list[DeviceInfo] = field(default_factory=list)
    quota_results: dict[str, Mapping[str, object] | Exception] = field(default_factory=dict)
    list_devices_calls: int = 0
    fetch_quota_calls: list[str] = field(default_factory=list)

    async def list_devices(self) -> list[DeviceInfo]:
        self.list_devices_calls += 1
        return list(self.devices)

    async def fetch_quota(self, sn: str) -> Mapping[str, object]:
        self.fetch_quota_calls.append(sn)
        result = self.quota_results.get(sn)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise KeyError(f"FakeDeviceCloud: no scripted quota result for {sn!r}")
        return result


__all__ = ["FakeClock", "FakeDeviceCloud"]
