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
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ecoflow_stats.acquisition.collector import run_forever
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle, run_derive_forever
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.routes.api import ApiContext
from ecoflow_stats.web.routes.api import router as api_router
from ecoflow_stats.web.routes.health import HealthContext
from ecoflow_stats.web.routes.health import router as health_router
from ecoflow_stats.web.routes.pages import PagesContext
from ecoflow_stats.web.routes.pages import router as pages_router
from ecoflow_stats.web.security import (
    AccessControlMiddleware,
    LoginThrottle,
    SecurityContext,
    SecurityHeadersMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from ecoflow_stats.bootstrap import Application

logger = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).resolve().parent / "static"


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
            derivation_store=application.derivation_store,
            live_states=application.live_outage_states,
            notification_service=application.notification_service,
            detector_config=DetectorConfig(
                threshold_v=application.settings.outage_threshold_v,
                gap_threshold_s=application.settings.gap_threshold,
            ),
        )

    supervised = SupervisedTask(name="collector", target=_run, clock=application.clock)
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def _start_derive_job(application: Application) -> SupervisedTaskHandle:
    """Production default: run the 5-minute derive job under supervision,
    started right alongside the collector (design module tree: "5-minute
    derive job"). `run_derive_forever` itself is fully implemented and
    tested in isolation (Phase 11 PR i); this was the one remaining,
    purely mechanical wiring gap -- it was never actually started from
    the real server process.
    """

    async def _run() -> None:
        await run_derive_forever(
            tuple(record.id for record in application.device_records),
            database=application.database,
            clock=application.clock,
            config=DetectorConfig(
                threshold_v=application.settings.outage_threshold_v,
                gap_threshold_s=application.settings.gap_threshold,
            ),
        )

    supervised = SupervisedTask(name="derive-job", target=_run, clock=application.clock)
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


def _build_api_context(application: Application) -> ApiContext:
    return ApiContext(
        now=application.clock.now,
        stale_threshold_s=application.settings.stale_threshold,
        poll_interval_s=application.settings.poll_interval,
        detector_config=DetectorConfig(
            threshold_v=application.settings.outage_threshold_v,
            gap_threshold_s=application.settings.gap_threshold,
        ),
        sample_store=application.sample_store,
        outage_store=OutageStore(application.database.writer),
        device_records=application.device_records,
        decision_store=DecisionStore(application.database.writer),
        tz=application.settings.tz,
    )


def _build_security_context(application: Application) -> SecurityContext:
    return SecurityContext(
        password=application.settings.password,
        allowed_networks=application.settings.allowed_networks,
        app_secret=application.secret,
        session_days=application.settings.session_days,
        now_s=lambda: int(application.clock.now().timestamp()),
        throttle=LoginThrottle(now=lambda: application.clock.now().timestamp()),
    )


def _build_pages_context(application: Application) -> PagesContext:
    return PagesContext(
        now=application.clock.now,
        stale_threshold_s=application.settings.stale_threshold,
        poll_interval_s=application.settings.poll_interval,
        detector_config=DetectorConfig(
            threshold_v=application.settings.outage_threshold_v,
            gap_threshold_s=application.settings.gap_threshold,
        ),
        sample_store=application.sample_store,
        outage_store=OutageStore(application.database.writer),
        device_records=application.device_records,
        default_lang=application.settings.default_lang,
    )


def create_app(
    application: Application,
    *,
    start_collector: Callable[[Application], SupervisedTaskHandle] = _start_collector,
    start_derive_job: Callable[[Application], SupervisedTaskHandle] = _start_derive_job,
) -> FastAPI:
    """Build the FastAPI app around `application`.

    `start_collector` and `start_derive_job` default to the real
    collector and derive job; tests pass a fake that starts a bounded,
    network-free task instead.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if application.settings.password is None:
            logger.warning(
                "access-control: no password configured (ECOFLOW_STATS_PASSWORD is unset); "
                "only the ECOFLOW_STATS_ALLOWED_NETWORKS LAN guard protects this instance"
            )
        handle = start_collector(application)
        derive_handle = start_derive_job(application)
        app.state.collector_handle = handle
        app.state.derive_job_handle = derive_handle
        app.state.application = application
        app.state.health = _build_health_context(application, handle)
        app.state.api = _build_api_context(application)
        app.state.pages = _build_pages_context(application)
        app.state.security = _build_security_context(application)
        try:
            yield
        finally:
            for running in (handle, derive_handle):
                running.task.cancel()
            for running in (handle, derive_handle):
                with contextlib.suppress(asyncio.CancelledError):
                    await running.task
            application.run_log.stop(application.run_id, int(application.clock.now().timestamp()))

    app = FastAPI(title="ecoflow-stats", lifespan=lifespan)
    app.add_middleware(AccessControlMiddleware)
    # Registered last so it wraps outermost: its headers (design,
    # "Headers") reach every response, including a 403/303 that
    # AccessControlMiddleware itself returns before routing even runs.
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health_router)
    app.include_router(api_router)
    app.include_router(pages_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app


__all__ = ["create_app"]
