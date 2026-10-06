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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ecoflow_stats.battery.service import observed_autonomy
from ecoflow_stats.battery.stats import (
    DailyBatteryTrend,
    battery_trend,
    charge_history,
    depth_of_discharge,
)
from ecoflow_stats.grid.quality import grid_quality_range
from ecoflow_stats.live_status.service import get_status
from ecoflow_stats.outages.aggregates import compute_aggregates
from ecoflow_stats.outages.resolve import resolve, unresolved_gaps
from ecoflow_stats.rollups.service import decode_energy_flags
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.timeutil import local_day

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.outages.aggregates import OutageAggregates
    from ecoflow_stats.outages.model import DetectorConfig, Gap
    from ecoflow_stats.outages.resolve import EffectiveOutage, EffectiveView
    from ecoflow_stats.ports import DecisionStore, OutageStore, SampleStore
    from ecoflow_stats.storage.devices import DeviceRecord
    from ecoflow_stats.storage.rollups import DailyEnergyRollup

_SCHEMA = "ecoflow-stats.status/v1"
_OUTAGES_SCHEMA = "ecoflow-stats.outages/v1"
_HEATMAP_SCHEMA = "ecoflow-stats.outages-heatmap/v1"
_GAPS_SCHEMA = "ecoflow-stats.gaps/v1"
_MAINS_STRIP_SCHEMA = "ecoflow-stats.mains-strip/v1"
_BATTERY_SERIES_SCHEMA = "ecoflow-stats.battery-series/v1"
_BATTERY_TRENDS_SCHEMA = "ecoflow-stats.battery-trends/v1"
_BATTERY_OUTAGES_SCHEMA = "ecoflow-stats.battery-outages/v1"
_GRID_SERIES_SCHEMA = "ecoflow-stats.grid-series/v1"
_GRID_DAILY_SCHEMA = "ecoflow-stats.grid-daily/v1"
_ENERGY_DAILY_SCHEMA = "ecoflow-stats.energy-daily/v1"
_DEFAULT_RANGE_S = 7 * 24 * 60 * 60
"""The range query defaults to the trailing 7 days when `from`/`to` are
omitted -- a sensible default for a dashboard call, not a domain rule."""
_PRESENT, _ABSENT, _UNKNOWN = "present", "absent", "unknown"

_MIN_TS = int(datetime.min.replace(tzinfo=UTC).timestamp())
_MAX_TS = int(datetime.max.replace(tzinfo=UTC).timestamp())
"""The representable-timestamp bound every `from`/`to` query param is
validated against (API-01). Bounded to `datetime`'s own min/max rather
than SQLite's wider signed-int64 column range -- `datetime`'s range is
the tighter of the two real crash sites this bound must cover:
`SampleStore.between()`'s raw SQLite bind (int64) and
`timeutil.local_day()`'s `datetime.fromtimestamp()` (platform `time_t`,
narrower still). Computed from a timezone-aware `datetime`, whose
`.timestamp()` is pure timedelta arithmetic against the epoch -- never
a platform `mktime`/`gmtime` call -- so computing the bound itself can
never raise the same `OverflowError` it exists to prevent."""

_GRID_BUCKET_TIERS: tuple[tuple[int | None, int], ...] = ((7, 300), (90, 3_600), (None, 86_400))
"""`grid/series`'s chart-resolution tiers (task 20.3): 5 min for a
range of 7 days or less, 1 h for up to 90 days, 1 day beyond that."""

_ENERGY_GRANULARITY_TIERS: tuple[tuple[int | None, str], ...] = ((90, "daily"), (None, "monthly"))
"""`energy/daily`'s aggregation-granularity tiers (task 19.1): daily
rows for a range of 90 days or less, monthly aggregates beyond that --
the same `_select_bucket` decision `grid/series` already applies at
its own thresholds and labels (task 20.5's dedup concern)."""


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
    tariff: float | None = None
    """The configured flat tariff price per kWh (`Settings.tariff`),
    `energy/daily`'s cost basis (task 19.1). `None` means no tariff is
    configured: that route reports cost as `"unavailable"`, never a
    fabricated `0` (energy requirement "Energy Cost from a Configurable
    Flat Tariff")."""
    currency: str = ""
    """The configured tariff's currency label (`Settings.currency`)."""


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
    """Validate and default `from`/`to` (API-01): the one shared place
    every `/api/v1/*` route taking a range calls, so an out-of-range or
    inverted pair is rejected with a clean `422` before any downstream
    call can raise the unhandled `OverflowError` the QA report caught at
    two different crash sites (`SampleStore.between()`,
    `timeutil.local_day()`)."""
    for name, value in (("from", start), ("to", end)):
        if value is not None and not (_MIN_TS <= value <= _MAX_TS):
            raise HTTPException(
                status_code=422,
                detail=f"{name!r} is out of the representable timestamp range",
            )
    range_end = end if end is not None else int(ctx.now().timestamp())
    range_start = start if start is not None else range_end - _DEFAULT_RANGE_S
    if range_start > range_end:
        raise HTTPException(status_code=422, detail="'from' must not be after 'to'")
    return range_start, range_end


def _select_bucket[BucketT](
    range_start: int, range_end: int, tiers: Sequence[tuple[int | None, BucketT]]
) -> BucketT:
    """Pick the coarsest tier whose day-span threshold still covers
    ``[range_start, range_end)``'s length -- the one reusable rule both
    `grid/series` (5min/1h/1day chart resolution) and `energy/daily`
    (daily-vs-monthly aggregation) apply at their own thresholds and
    labels (task 20.5's dedup concern: a single shared decision, not an
    independently maintained copy per route). ``tiers`` is ordered
    ``(max_days, value)``; ``max_days=None`` is the open-ended "beyond
    every prior threshold" tier and must be last."""
    span_days = (range_end - range_start) / 86_400
    for max_days, value in tiers:
        if max_days is None or span_days <= max_days:
            return value
    return tiers[-1][1]


def _utc_day_str(ts: int) -> str:
    """UTC calendar day for ``ts`` -- the same placeholder day-
    bucketing `rollups.service._utc_day` uses (Phase 18 will replace
    both with a timezone-aware `local_day`; duplicated here as a
    one-line conversion rather than importing that module's private
    helper)."""
    return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()


def _rollup_store(request: Request) -> RollupStore:
    """`RollupStore` built from the real `Application`'s own writer
    connection, read cross-context from `request.app.state.application`
    -- the same `bootstrap.build`-level object `web.app.create_app`
    already stashes there -- rather than adding a new `ApiContext`
    field: `web/app.py`'s `_build_api_context` is outside this slice's
    declared edit surface, so this avoids touching it, the same
    cross-context-read choice `web.routes.pages` already uses for
    `request.app.state.security`/`request.app.state.api`
    (apply-progress-batch14)."""
    application = request.app.state.application
    return RollupStore(application.database.writer)


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


def _clip(start: int, end: int | None, range_start: int, range_end: int) -> tuple[int, int] | None:
    clipped_start = max(start, range_start)
    clipped_end = min(end if end is not None else range_end, range_end)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def _paint(
    segments: list[tuple[int, int, str]], start: int, end: int, state: str
) -> list[tuple[int, int, str]]:
    """Overwrite `[start, end)` with `state` across `segments`, splitting
    any segment it partially overlaps. Later paints win over earlier
    ones -- the priority order `_build_mains_strip` relies on."""
    painted: list[tuple[int, int, str]] = []
    for seg_start, seg_end, seg_state in segments:
        if seg_end <= start or seg_start >= end:
            painted.append((seg_start, seg_end, seg_state))
            continue
        if seg_start < start:
            painted.append((seg_start, start, seg_state))
        if seg_end > end:
            painted.append((end, seg_end, seg_state))
    painted.append((start, end, state))
    painted.sort(key=lambda segment: segment[0])
    return painted


def _merge_adjacent(segments: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for segment in segments:
        if merged and merged[-1][1] == segment[0] and merged[-1][2] == segment[2]:
            prev_start, _prev_end, state = merged[-1]
            merged[-1] = (prev_start, segment[1], state)
        else:
            merged.append(segment)
    return merged


def _build_mains_strip(
    outages: Sequence[EffectiveOutage],
    gaps: Sequence[Gap],
    range_start: int,
    range_end: int,
) -> list[tuple[int, int, str]]:
    """Turn confirmed outages and raw gaps into one gapless present/
    absent/unknown timeline (design "Visual language": present solid, an
    outage a cut, unknown time hatched -- the app's "a gap is not an
    outage by inference" rule, shown visually).

    Painted in priority order, lowest first: the whole range starts
    `present`; every raw gap paints `unknown` over it (a silent window is
    never known to be present); every *confirmed* outage then paints
    `absent` over both -- so a gap a user has since confirmed as a real
    outage renders as a solid cut, not hatched, even though the same
    window is also a `Gap` row in storage.
    """
    if range_end <= range_start:
        return []
    segments: list[tuple[int, int, str]] = [(range_start, range_end, _PRESENT)]
    for gap in gaps:
        clipped = _clip(gap.start_ts, gap.end_ts, range_start, range_end)
        if clipped is not None:
            segments = _paint(segments, clipped[0], clipped[1], _UNKNOWN)
    for outage in outages:
        clipped = _clip(outage.start_ts, outage.end_ts, range_start, range_end)
        if clipped is not None:
            segments = _paint(segments, clipped[0], clipped[1], _ABSENT)
    return _merge_adjacent(segments)


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


@router.get("/api/v1/mains-strip")
def mains_strip_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """The mains strip's data source: a run-length `[start, end, state]`
    series covering the whole requested range with no holes."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    view = _reconcile(ctx, device_id, range_start, range_end)
    gaps = ctx.outage_store.gaps(device_id, range_start, range_end)
    segments = _build_mains_strip(view.outages, gaps, range_start, range_end)
    body = {
        "schema": _MAINS_STRIP_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "series": [[seg_start, seg_end, state] for seg_start, seg_end, state in segments],
    }
    return JSONResponse(body)


@router.get("/api/v1/battery/series")
def battery_series_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """battery requirement "Charge History": the charge line's data
    source."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    samples = [
        (row.ts, row.reading) for row in ctx.sample_store.between(device_id, range_start, range_end)
    ]
    points = charge_history(samples)
    body = {
        "schema": _BATTERY_SERIES_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "points": [{"ts": point.ts, "soc": point.soc} for point in points],
    }
    return JSONResponse(body)


@router.get("/api/v1/battery/trends")
def battery_trends_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """battery requirement "Cycle Count and State-of-Health Trends"."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    rollup_rows = _rollup_store(request).between(
        device_id, _utc_day_str(range_start), _utc_day_str(range_end)
    )
    trend_days = [
        DailyBatteryTrend(
            day=row.day,
            cycles_last=row.cycles_last,
            soh_last=row.soh_last,
            soc_min=row.soc_min,
            soc_max=row.soc_max,
            batt_temp_max=row.batt_temp_max,
        )
        for row in rollup_rows
    ]
    trend = battery_trend(trend_days)
    body = {
        "schema": _BATTERY_TRENDS_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "days": [
            {"day": point.day, "cycles_last": point.cycles_last, "soh_last": point.soh_last}
            for point in trend
        ],
    }
    return JSONResponse(body)


@router.get("/api/v1/battery/outages")
def battery_outages_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """battery requirements "Depth of Discharge Per Outage" and
    "Observed Autonomy Compared With Device Estimate" -- per-outage
    depth of discharge and observed autonomy, NULL-safe on both
    amendments 6 and 7. Only confirmed outages (`kind == "outage"`)
    are considered: a brief event, a user-confirmed gap, or an
    imported legacy entry carries no `soc_min`/`dsg_remain_min_start`
    telemetry at all, so both functions would always report
    "unavailable"/"not enough data" for them anyway."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    events = [
        event
        for event in ctx.outage_store.events(device_id, range_start, range_end)
        if event.kind == "outage"
    ]
    rows = []
    for event in events:
        dod = depth_of_discharge(event)
        autonomy = observed_autonomy(event)
        rows.append(
            {
                "start_ts": event.start_ts,
                "end_ts": event.end_ts,
                "depth_of_discharge": dod,
                "observed_autonomy": (
                    "not enough data"
                    if autonomy == "not enough data"
                    else {
                        "observed_h": autonomy.observed_h,
                        "device_estimate_h": autonomy.device_estimate_h,
                    }
                ),
            }
        )
    body = {
        "schema": _BATTERY_OUTAGES_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "outages": rows,
    }
    return JSONResponse(body)


@router.get("/api/v1/grid/series")
def grid_series_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """grid-quality requirement "Grid Voltage and Frequency History":
    the chart's bucketed data source, bucketed at 5 min, 1 h, or 1 day
    depending on the requested range's length (task 20.3), computed
    fresh from raw samples rather than from the persisted daily
    rollups -- `grid/daily` below serves those."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    bucket_width_s = _select_bucket(range_start, range_end, _GRID_BUCKET_TIERS)
    buckets: dict[int, list[tuple[int, Reading]]] = {}
    for row in ctx.sample_store.between(device_id, range_start, range_end):
        bucket_start = range_start + ((row.ts - range_start) // bucket_width_s) * bucket_width_s
        buckets.setdefault(bucket_start, []).append((row.ts, row.reading))
    points = []
    for bucket_start in sorted(buckets):
        quality = grid_quality_range(buckets[bucket_start])
        if quality == "unavailable":
            continue
        points.append(
            {
                "ts": bucket_start,
                "grid_v_min": quality.grid_v_min,
                "grid_v_avg": quality.grid_v_avg,
                "grid_v_max": quality.grid_v_max,
                "grid_hz_min": quality.grid_hz_min,
                "grid_hz_avg": quality.grid_hz_avg,
                "grid_hz_max": quality.grid_hz_max,
            }
        )
    body = {
        "schema": _GRID_SERIES_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "bucket_width_s": bucket_width_s,
        "points": points,
    }
    return JSONResponse(body)


@router.get("/api/v1/grid/daily")
def grid_daily_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """grid-quality requirement "Daily Voltage and Frequency Ranges":
    the persisted per-day min/avg/max rows `rollups.service.
    derive_rollups` already wrote via `RollupStore.upsert_grid` (task
    20.2's rollup half)."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    rows = _rollup_store(request).grid_between(
        device_id, local_day(range_start, ctx.tz), local_day(range_end, ctx.tz)
    )
    body = {
        "schema": _GRID_DAILY_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "days": [
            {
                "day": row.day,
                "grid_v_min": row.grid_v_min,
                "grid_v_avg": row.grid_v_avg,
                "grid_v_max": row.grid_v_max,
                "grid_hz_min": row.grid_hz_min,
                "grid_hz_avg": row.grid_hz_avg,
                "grid_hz_max": row.grid_hz_max,
                "readings": row.grid_readings,
            }
            for row in rows
        ],
    }
    return JSONResponse(body)


def _energy_period(
    period: str,
    chg_ac_wh: float,
    chg_dc_wh: float,
    chg_solar_wh: float,
    dsg_ac_wh: float,
    dsg_dc_wh: float,
    chg_ac_est_wh: float,
    flags: list[str],
    *,
    tariff: float | None,
    currency: str,
) -> dict[str, object]:
    return {
        "period": period,
        "chg_ac_wh": chg_ac_wh,
        "chg_dc_wh": chg_dc_wh,
        "chg_solar_wh": chg_solar_wh,
        "dsg_ac_wh": dsg_ac_wh,
        "dsg_dc_wh": dsg_dc_wh,
        "chg_ac_est_wh": chg_ac_est_wh,
        "cost": "unavailable" if tariff is None else chg_ac_wh / 1000 * tariff,
        "currency": currency,
        "flags": flags,
    }


def _build_energy_periods(
    rows: Sequence[DailyEnergyRollup], granularity: str, *, tariff: float | None, currency: str
) -> list[dict[str, object]]:
    """Daily or monthly energy periods from a device's persisted energy
    rollup rows (energy requirement "Daily Energy by Source and
    Direction"; task 19.1). Each row's missing Wh fields (a day
    `derive_rollups` never ran `upsert_energy` for at all) default to
    `0.0` -- the same "no energy moved" semantics
    `energy.accounting.DailyEnergy`'s own numeric defaults already use,
    distinct from `cost`'s own separate `"unavailable"` sentinel."""
    if granularity == "monthly":
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            month = row.day[:7]
            bucket = grouped.setdefault(
                month,
                {
                    "chg_ac_wh": 0.0,
                    "chg_dc_wh": 0.0,
                    "chg_solar_wh": 0.0,
                    "dsg_ac_wh": 0.0,
                    "dsg_dc_wh": 0.0,
                    "chg_ac_est_wh": 0.0,
                    "flags": set(),
                },
            )
            bucket["chg_ac_wh"] += row.chg_ac_wh or 0.0  # type: ignore[operator]
            bucket["chg_dc_wh"] += row.chg_dc_wh or 0.0  # type: ignore[operator]
            bucket["chg_solar_wh"] += row.chg_solar_wh or 0.0  # type: ignore[operator]
            bucket["dsg_ac_wh"] += row.dsg_ac_wh or 0.0  # type: ignore[operator]
            bucket["dsg_dc_wh"] += row.dsg_dc_wh or 0.0  # type: ignore[operator]
            bucket["chg_ac_est_wh"] += row.chg_ac_est_wh  # type: ignore[operator]
            bucket["flags"] |= decode_energy_flags(row.energy_flags)  # type: ignore[operator]
        return [
            _energy_period(
                month,
                b["chg_ac_wh"],  # type: ignore[arg-type]
                b["chg_dc_wh"],  # type: ignore[arg-type]
                b["chg_solar_wh"],  # type: ignore[arg-type]
                b["dsg_ac_wh"],  # type: ignore[arg-type]
                b["dsg_dc_wh"],  # type: ignore[arg-type]
                b["chg_ac_est_wh"],  # type: ignore[arg-type]
                sorted(b["flags"]),  # type: ignore[arg-type]
                tariff=tariff,
                currency=currency,
            )
            for month, b in sorted(grouped.items())
        ]
    return [
        _energy_period(
            row.day,
            row.chg_ac_wh or 0.0,
            row.chg_dc_wh or 0.0,
            row.chg_solar_wh or 0.0,
            row.dsg_ac_wh or 0.0,
            row.dsg_dc_wh or 0.0,
            row.chg_ac_est_wh,
            sorted(decode_energy_flags(row.energy_flags)),
            tariff=tariff,
            currency=currency,
        )
        for row in rows
    ]


@router.get("/api/v1/energy/daily")
def energy_daily_route(
    request: Request,
    device: int | None = None,
    start: int | None = Query(None, alias="from"),
    end: int | None = Query(None, alias="to"),
) -> JSONResponse:
    """energy requirements "Daily Energy by Source and Direction",
    "Energy Cost from a Configurable Flat Tariff": daily (or monthly
    beyond 90 days) kWh by flow, cost per period and total, and each
    row's estimate flags, read from the persisted energy rollup columns
    fix01 wired `derive_rollups` to write (task 19.1's API half)."""
    ctx: ApiContext = request.app.state.api
    device_id = _resolve_device_id(ctx, device)
    range_start, range_end = _resolve_range(ctx, start, end)
    granularity = _select_bucket(range_start, range_end, _ENERGY_GRANULARITY_TIERS)
    rows = _rollup_store(request).energy_between(
        device_id, local_day(range_start, ctx.tz), local_day(range_end, ctx.tz)
    )
    periods = _build_energy_periods(rows, granularity, tariff=ctx.tariff, currency=ctx.currency)
    total_chg_ac_wh = sum(period["chg_ac_wh"] for period in periods)  # type: ignore[misc]
    body = {
        "schema": _ENERGY_DAILY_SCHEMA,
        "device_id": device_id,
        "range": {"start": range_start, "end": range_end},
        "granularity": granularity,
        "periods": periods,
        "total": {
            "chg_ac_wh": total_chg_ac_wh,
            "cost": "unavailable" if ctx.tariff is None else total_chg_ac_wh / 1000 * ctx.tariff,
            "currency": ctx.currency,
        },
    }
    return JSONResponse(body)


__all__ = [
    "ApiContext",
    "battery_outages_route",
    "battery_series_route",
    "battery_trends_route",
    "energy_daily_route",
    "gaps_route",
    "grid_daily_route",
    "grid_series_route",
    "mains_strip_route",
    "outages_heatmap_route",
    "outages_route",
    "router",
    "status_route",
]
