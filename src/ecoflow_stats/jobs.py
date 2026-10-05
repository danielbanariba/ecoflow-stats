"""Task supervision: run one long-lived async job forever, restarting it
with doubling backoff if it ever crashes, and exposing enough state for
``/healthz`` to report on once that exists (Phase 7).

The collector and, later, the 5-minute derive job each run under their own
:class:`SupervisedTask` so that one crashed task is retried rather than
silently stopping collection for good (acquisition: a tick failure must
not stop future ticks).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ecoflow_stats.ports import Clock

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_S = 5.0
_MAX_BACKOFF_S = 300.0


@dataclass
class SupervisedTask:
    """Runs ``target`` forever; if it raises (other than cancellation),
    waits a backoff — doubling from 5s up to a 5-minute cap — and restarts
    it. A clean return from ``target`` (it is not expected to return) also
    restarts it after the same backoff, so the job is never silently
    abandoned."""

    name: str
    target: Callable[[], Awaitable[None]]
    clock: Clock
    restart_count: int = field(default=0, init=False)
    running: bool = field(default=False, init=False)
    last_error: str | None = field(default=None, init=False)

    async def run(self) -> None:
        """Run until cancelled. Intended to be wrapped in its own
        ``asyncio.Task`` by the caller (the application lifespan)."""
        backoff = _INITIAL_BACKOFF_S
        while True:
            self.running = True
            try:
                await self.target()
            except asyncio.CancelledError:
                self.running = False
                raise
            except Exception as exc:  # noqa: BLE001 — a supervisor must survive any crash
                self.running = False
                self.last_error = str(exc)
                self.restart_count += 1
                logger.warning(
                    "supervised task %r crashed (%s); restarting in %.0fs",
                    self.name,
                    exc,
                    backoff,
                )
                await self.clock.sleep_until(self.clock.now() + timedelta(seconds=backoff))
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
            else:
                self.running = False
                backoff = _INITIAL_BACKOFF_S


__all__ = ["SupervisedTask"]
