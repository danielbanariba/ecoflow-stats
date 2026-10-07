"""The real clock adapter: the only place the application reads wall-clock time.

Everything else — the collector, the outage state machine, the derive job —
takes a ``ports.Clock`` and never calls ``datetime.now()`` directly, so tests
can substitute a deterministic fake instead of racing the real clock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


class SystemClock:
    """Real time source; the production implementation of ``ports.Clock``."""

    def now(self) -> datetime:
        """Return the current, timezone-aware UTC time."""
        return datetime.now(UTC)

    async def sleep_until(self, when: datetime) -> None:
        """Suspend until ``when``; return immediately if ``when`` has passed."""
        delay = (when - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


__all__ = ["SystemClock"]
