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

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ecoflow_stats.live_status.service import get_status
from ecoflow_stats.outages.aggregates import compute_aggregates
from ecoflow_stats.outages.resolve import resolve, unresolved_gaps

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ecoflow_stats.outages.aggregates import OutageAggregates
    from ecoflow_stats.outages.model import DetectorConfig, Gap
    from ecoflow_stats.outages.resolve import EffectiveView
    from ecoflow_stats.ports import DecisionStore, OutageStore, SampleStore
    from ecoflow_stats.storage.devices import DeviceRecord

_SCHEMA = "ecoflow-stats.status/v1"
_OUTAGES_SCHEMA = "ecoflow-stats.outages/v1"
_HEATMAP_SCHEMA = "ecoflow-stats.outages-heatmap/v1"
_GAPS_SCHEMA = "ecoflow-stats.gaps/v1"
_DEFAULT_RANGE_S = 7 * 24 * 60 * 60
"""The range query defaults to the trailing 7 days when `from`/`to` are
omitted -- a sensible default for a dashboard call, not a domain rule."""


@dataclass(frozen=True, slots=True)
class ApiContext:
    """Everything the `/api/v1` routes read, attached to `app.state.api`."""

    now: Callable[[], datetime]
    stale_threshold_s: int
    poll_interval_s: int
    detector_config: DetectorConfig
    sample_store: SampleStore
    outage_store: OutageStore
    device_records: tuple[DeviceRecord, ...]
    decision_store: DecisionStore | None = None
    """`None` when no decision store is wired (never happens in
    production -- `web.app._build_api_context` always wires the real
    one); treated as "no active decisions yet" rather than failing, the
    same honest-default style as `resolve()`'s own `legacy=()` here."""
    tz: str = "UTC"


router = APIRouter()


def _resolve_device_id(ctx: ApiContext, requested: int | None) -> int:
    """An explicitly requested, configured device id wins; otherwise the
    first configured device -- the same fallback `web.deps.select_device_id`
    uses for pages, without that function's cookie coupling (this is a
    stateless JSON API, never a browser session)."""
    device_ids = tuple(record.id for record in ctx.device_records)
    if requested is not None and requested in device_ids:
        return requested
    return device_ids[0]


def _resolve_range(ctx: ApiContext, start: int | None, end: int | None) -> tuple[int, int]:
    range_end = end if end is not None else int(ctx.now().timestamp())
    range_start = start if start is not None else range_end - _DEFAULT_RANGE_S
    return range_start, range_end


def _reconcile(ctx: ApiContext, device_id: int, range_start: int, range_end: int) -> EffectiveView:
    """Fetch this device's detected events and gaps for `[range_start,
    range_end]`, and every active decision, then hand them to Phase 11's
    `resolve()` -- the same reconciliation pipeline that module's own unit
    tests already exercise.

    `legacy` is always empty: `storage.legacy.LegacyStore` currently has
    no range query (only `get(device_id, start_ts)`, an exact lookup), so
    no production caller can fetch "every legacy entry overlapping a
    range" yet. Extending it is a storage-layer change outside this
    route's edit surface; tracked as a known gap (apply-progress)."""
    events = ctx.outage_store.events(device_id, range_start, range_end)
    gaps = ctx.outage_store.gaps(device_id, range_start, range_end)
    decisions = ctx.decision_store.active(device_id) if ctx.decision_store is not None else []
    return resolve(detected=events, gaps=gaps, legacy=(), decisions=decisions, range_end=range_end)


def _compute_outage_aggregates(
    ctx: ApiContext, device_id: int, range_start: int, range_end: int
) -> OutageAggregates:
    view = _reconcile(ctx, device_id, range_start, range_end)
    return compute_aggregates(
        outages=view.outages, range_start=range_start, range_end=range_end, tz=ctx.tz
    )


def _serialize_gap(gap: Gap) -> dict[str, object]:
    return {
        "start_ts": gap.start_ts,
        "end_ts": gap.end_ts,
        "cause": gap.cause,
        "state_before": gap.state_before,
        "state_after": gap.state_after,
        "soc_before": gap.soc_before,
        "soc_after": gap.soc_after,
        "chg_ac_wh_delta": gap.chg_ac_wh_delta,
        "expected_in_wh": gap.expected_in_wh,
        "evidence": gap.evidence,
        "failures": dict(gap.failures),
    }


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


@router.get("/api/v1/outages")
def outages_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """outages requirement "Outage Aggregates": count, total downtime and
    longest single outage for a device/range, reconciled through Phase
    11's `resolve()` so a user-confirmed gap or imported legacy entry
    counts exactly once, never twice and never silently."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    aggregates = _compute_outage_aggregates(ctx, device_id, range_start, range_end)
    body = {
        "schema": _OUTAGES_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "count": aggregates.count,
        "total_downtime_s": aggregates.total_downtime_s,
        "longest_s": aggregates.longest_s,
    }
    return JSONResponse(body)


@router.get("/api/v1/outages/heatmap")
def outages_heatmap_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """The weekday/hour distribution of confirmed outage start times --
    the same `OutageAggregates` the `outages` route above computes, this
    route just exposes its two distribution arrays instead of its
    totals."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    aggregates = _compute_outage_aggregates(ctx, device_id, range_start, range_end)
    body = {
        "schema": _HEATMAP_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "hour_of_day": aggregates.hour_of_day,
        "day_of_week": aggregates.day_of_week,
    }
    return JSONResponse(body)


@router.get("/api/v1/gaps")
def gaps_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """The gap review queue (web-ui "Gap and Phantom Review Flow"): every
    gap in range with no active decision yet, each carrying the
    before/after evidence `evidence.make_gap` already computed, so a
    reviewer never has to guess."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    gaps = ctx.outage_store.gaps(device_id, range_start, range_end)
    decisions = ctx.decision_store.active(device_id) if ctx.decision_store is not None else []
    pending = unresolved_gaps(gaps, decisions, range_end)
    body = {
        "schema": _GAPS_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "gaps": [_serialize_gap(gap) for gap in pending],
    }
    return JSONResponse(body)


__all__ = [
    "ApiContext",
    "gaps_route",
    "outages_heatmap_route",
    "outages_route",
    "router",
    "status_route",
]
