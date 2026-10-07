"""`GET /healthz`: unauthenticated, read-only, reflects only what a
restart can fix (design D13; spec amendment A1, which replaces the
original "unhealthy when last-sample age exceeds threshold" requirement).

`evaluate_status` and the 503 trigger are deliberately independent: an
EcoFlow cloud outage ages every sample but must only ever reach
`degraded`, never 503 — restarting the container cannot fix a cloud
outage. 503 is reserved for the collector task being dead or the
database being unwritable, which a restart *can* fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

HealthStatus = Literal["ok", "degraded", "starting"]
CollectorState = Literal["running", "restarting"]


@dataclass(frozen=True, slots=True)
class HealthContext:
    """Everything `GET /healthz` reads, attached to `app.state.health`.

    Every field is a plain value or a callable rather than a live object
    (a `Database`, a `SupervisedTaskHandle`, ...), so this module needs no
    FastAPI-unrelated import and the route is testable with hand-built
    stubs, no real collector or database required.
    """

    now_s: Callable[[], int]
    stale_threshold_s: int
    collector_alive: Callable[[], bool]
    collector_running: Callable[[], bool]
    db_writable: Callable[[], bool]
    device_ids: tuple[int, ...]
    last_sample_ts: Callable[[int], int | None]


def evaluate_status(device_ages_s: Sequence[int | None], *, stale_threshold_s: int) -> HealthStatus:
    """Pure: `ok`/`degraded`/`starting` from each device's last-sample age.

    `None` means that device has no sample yet at all. `starting` if any
    device has none (including "no devices configured yet" — an empty
    sequence); `degraded` once every device has sampled at least once but
    any is older than `stale_threshold_s`; `ok` otherwise.
    """
    if not device_ages_s or any(age is None for age in device_ages_s):
        return "starting"
    if any(age > stale_threshold_s for age in device_ages_s):
        return "degraded"
    return "ok"


router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    ctx: HealthContext = request.app.state.health
    now = ctx.now_s()
    ages: list[int | None] = []
    devices: list[dict[str, int | None]] = []
    for device_id in ctx.device_ids:
        ts = ctx.last_sample_ts(device_id)
        age = None if ts is None else now - ts
        ages.append(age)
        devices.append({"id": device_id, "last_sample_age_s": age})

    status = evaluate_status(ages, stale_threshold_s=ctx.stale_threshold_s)
    collector_state: CollectorState = "running" if ctx.collector_running() else "restarting"
    healthy = ctx.collector_alive() and ctx.db_writable()
    body = {"status": status, "collector": collector_state, "devices": devices}
    return JSONResponse(body, status_code=200 if healthy else 503)


__all__ = [
    "CollectorState",
    "HealthContext",
    "HealthStatus",
    "evaluate_status",
    "healthz",
    "router",
]
