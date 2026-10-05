"""The FastAPI composition shell: builds the app around an already-built
`Application` (`bootstrap.build`), starting the collector under
supervision on startup and stopping it cleanly on shutdown.

Starting the collector cannot happen in `bootstrap.build` itself —
`asyncio.create_task` needs a running event loop, which does not exist
until the ASGI server (or a test's `TestClient`) drives this lifespan.
`start_collector` is injectable so a test can prove the startup/shutdown
lifecycle without a real network call or an unboundable loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from ecoflow_stats.acquisition.collector import run_forever
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.routes.health import HealthContext
from ecoflow_stats.web.routes.health import router as health_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from ecoflow_stats.bootstrap import Application


def _start_collector(application: Application) -> SupervisedTaskHandle:
    """Production default: run the real collector loop under supervision."""

    async def _run() -> None:
        await run_forever(
            application.collector_devices,
            cloud=application.cloud,
            registry=application.registry,
            samples=application.sample_store,
            failures=application.failure_log,
            clock=application.clock,
            poll_interval_s=application.settings.poll_interval,
            poll_offset_s=application.settings.poll_offset,
        )

    supervised = SupervisedTask(name="collector", target=_run, clock=application.clock)
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def _build_health_context(application: Application, handle: SupervisedTaskHandle) -> HealthContext:
    return HealthContext(
        now_s=lambda: int(application.clock.now().timestamp()),
        stale_threshold_s=application.settings.stale_threshold,
        collector_alive=lambda: handle.alive,
        collector_running=lambda: handle.running,
        db_writable=application.database.is_writable,
        device_ids=tuple(record.id for record in application.device_records),
        last_sample_ts=lambda device_id: (
            sample.ts if (sample := application.sample_store.latest(device_id)) else None
        ),
    )


def create_app(
    application: Application,
    *,
    start_collector: Callable[[Application], SupervisedTaskHandle] = _start_collector,
) -> FastAPI:
    """Build the FastAPI app around `application`.

    `start_collector` defaults to the real collector; tests pass a fake
    that starts a bounded, network-free task instead.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        handle = start_collector(application)
        app.state.collector_handle = handle
        app.state.application = application
        app.state.health = _build_health_context(application, handle)
        try:
            yield
        finally:
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.task
            application.run_log.stop(application.run_id, int(application.clock.now().timestamp()))

    app = FastAPI(title="ecoflow-stats", lifespan=lifespan)
    app.include_router(health_router)
    return app


__all__ = ["create_app"]
