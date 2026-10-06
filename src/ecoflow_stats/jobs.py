"""Task supervision: run one long-lived async job forever, restarting it
with doubling backoff if it ever crashes, and exposing enough state for
``/healthz`` to report on once that exists (Phase 7).

The collector, the 5-minute derive job, and the hourly rollups job
(visual-QA batch fix01, fix 5) each run under their own
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

from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.outages.service import derive_outages
from ecoflow_stats.rollups.service import derive_rollups
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.storage.runs import RunLog
from ecoflow_stats.storage.samples import SampleStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from ecoflow_stats.ports import Clock
    from ecoflow_stats.storage.database import Database

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_S = 5.0
_MAX_BACKOFF_S = 300.0
_DEFAULT_DERIVE_CONFIG = DetectorConfig()


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


@dataclass(frozen=True, slots=True)
class SupervisedTaskHandle:
    """A started :class:`SupervisedTask` plus the real ``asyncio.Task``
    driving it, so a caller (the health route) can ask whether the
    supervisor itself is still alive without reaching into asyncio
    internals directly.

    ``alive`` and ``running`` answer different questions: ``running`` is
    legitimately ``False`` during an ordinary crash backoff the supervisor
    will recover from on its own; ``alive`` is ``False`` only once the
    wrapping task has actually ended (a clean shutdown via cancellation, or
    a crash kind ``SupervisedTask.run`` itself cannot catch). The health
    check's 503 trigger (amendment A1: "the collector task is dead") is
    ``alive``, never ``running``.
    """

    supervised: SupervisedTask
    task: asyncio.Task[None]

    @property
    def running(self) -> bool:
        return self.supervised.running

    @property
    def alive(self) -> bool:
        return not self.task.done()


async def run_derive_forever(
    device_ids: Sequence[int],
    *,
    database: Database,
    clock: Clock,
    config: DetectorConfig = _DEFAULT_DERIVE_CONFIG,
    interval_s: float = 300.0,
) -> None:
    """Recompute outages for every configured device every ``interval_s``
    seconds (design module tree: "5-minute derive job"), forever, until
    cancelled.

    Builds its stores once, over the shared writer connection, and
    isolates one device's recompute failure from the rest -- mirroring
    the collector's own per-device isolation (acquisition requirement: a
    tick failure must not stop future ticks) -- so one device's bug never
    starves the others, or the next tick, of a recompute. Intended to run
    under its own :class:`SupervisedTask`, which already restarts it (with
    backoff) if this coroutine itself ever raises past the per-device
    guard below.
    """
    sample_store = SampleStore(database.writer)
    failure_log = FailureLog(database.writer)
    run_log = RunLog(database.writer)
    outage_store = OutageStore(database.writer)
    derivation_store = DerivationStore(database.writer)
    while True:
        now = clock.now()
        for device_id in device_ids:
            database.writer.execute("BEGIN IMMEDIATE")
            try:
                derive_outages(
                    device_id,
                    sample_store=sample_store,
                    failure_log=failure_log,
                    run_log=run_log,
                    outage_store=outage_store,
                    derivation_store=derivation_store,
                    config=config,
                    now=now,
                )
            except asyncio.CancelledError:
                database.writer.rollback()
                raise
            except Exception:
                database.writer.rollback()
                logger.exception("derive job failed for device id %s", device_id)
            else:
                database.writer.commit()
        await clock.sleep_until(clock.now() + timedelta(seconds=interval_s))


async def run_rollups_forever(
    device_ids: Sequence[int],
    *,
    database: Database,
    clock: Clock,
    tz: str = "UTC",
    interval_s: float = 3600.0,
) -> None:
    """Recompute daily rollups for every configured device every
    ``interval_s`` seconds (default: hourly), forever, until cancelled.

    Visual-QA batch fix01, fix 5: `rollups.service.derive_rollups` was
    fully implemented and tested in isolation but had zero production
    callers -- in a real deployment, `daily_rollups` would stay empty
    forever, and the battery page's DoD/trend/autonomy tables would
    show no data despite working perfectly in a manual test. An hourly
    cadence is cheap after each device's first run: `derive_rollups`'s
    own dirty-day tracking (`storage.derivations.DerivationStore`) only
    recomputes days from the dirty mark onward, never the whole history
    again, once a device is no longer in its very first full recompute.

    Same per-device isolation and own-transaction-per-device shape as
    `run_derive_forever`, so one device's recompute bug never starves
    the others, or the next tick, of a recompute.
    """
    sample_store = SampleStore(database.writer)
    rollup_store = RollupStore(database.writer)
    derivation_store = DerivationStore(database.writer)
    while True:
        now = clock.now()
        for device_id in device_ids:
            database.writer.execute("BEGIN IMMEDIATE")
            try:
                derive_rollups(
                    device_id,
                    sample_store=sample_store,
                    rollup_store=rollup_store,
                    derivation_store=derivation_store,
                    now=now,
                    tz=tz,
                )
            except asyncio.CancelledError:
                database.writer.rollback()
                raise
            except Exception:
                database.writer.rollback()
                logger.exception("rollups job failed for device id %s", device_id)
            else:
                database.writer.commit()
        await clock.sleep_until(clock.now() + timedelta(seconds=interval_s))


__all__ = [
    "SupervisedTask",
    "SupervisedTaskHandle",
    "run_derive_forever",
    "run_rollups_forever",
]
