"""Test doubles shared across the suite.

Seeded here with ``FakeClock`` only; later work units add ``FakeDeviceCloud``
and ``RecordingNotifier`` as the phases that need them land (the collector
and notification tests). Triangulation skipped: this is test infrastructure,
not a behavior under test — it has no Phase 1 consumer yet and is exercised
indirectly once the collector's scheduling tests drive it.
"""

from __future__ import annotations

from datetime import datetime


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


__all__ = ["FakeClock"]
