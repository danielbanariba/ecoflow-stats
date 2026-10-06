"""`GET /api/v1/status`: the versioned, read-only live-status contract
(live-status requirement: "Versioned Read-Only Status Endpoint").

Every field reaches the response from a plain value or callable, never
a live object with its own behavior beyond what `ApiContext` exposes —
the same reasoning `web.routes.health.HealthContext` already documents,
so this route is testable with hand-built stubs and real SQLite stores,
no real collector or composition root required.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ecoflow_stats.live_status.service import get_status

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ecoflow_stats.outages.model import DetectorConfig
    from ecoflow_stats.ports import OutageStore, SampleStore
    from ecoflow_stats.storage.devices import DeviceRecord

_SCHEMA = "ecoflow-stats.status/v1"


@dataclass(frozen=True, slots=True)
class ApiContext:
    """Everything `GET /api/v1/status` reads, attached to `app.state.api`."""

    now: Callable[[], datetime]
    stale_threshold_s: int
    poll_interval_s: int
    detector_config: DetectorConfig
    sample_store: SampleStore
    outage_store: OutageStore
    device_records: tuple[DeviceRecord, ...]


router = APIRouter()


@router.get("/api/v1/status")
def status_route(request: Request) -> JSONResponse:
    ctx: ApiContext = request.app.state.api
    now = ctx.now()
    devices = []
    for record in ctx.device_records:
        status = get_status(
            record.id,
            sample_store=ctx.sample_store,
            outage_store=ctx.outage_store,
            now=now,
            stale_threshold_s=ctx.stale_threshold_s,
            poll_interval_s=ctx.poll_interval_s,
            config=ctx.detector_config,
        )
        devices.append(
            {
                "id": record.id,
                "name": record.name,
                "serial_tail": record.sn[-4:],
                "adapter": record.adapter_id,
                "ts": status.ts,
                "age_s": status.age_s,
                "stale": status.stale,
                "grid": status.grid,
                "outage": {"ongoing": status.outage.ongoing, "since": status.outage.since},
                "reading": (
                    dataclasses.asdict(status.reading) if status.reading is not None else None
                ),
            }
        )
    body = {
        "schema": _SCHEMA,
        "generated_at": int(now.timestamp()),
        "devices": devices,
    }
    return JSONResponse(body)


__all__ = ["ApiContext", "router", "status_route"]
