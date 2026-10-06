"""Pure view-model builders for the web pages.

Kept separate from route handlers so "what does this page show" is
testable without FastAPI, Jinja2, or storage: every function here takes
already-fetched data (a `DeviceStatus`, device records) and returns
plain dataclasses the templates render directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ecoflow_stats.battery.service import observed_autonomy
from ecoflow_stats.battery.stats import (
    battery_trend,
    bucket_charge_history,
    charge_history,
    depth_of_discharge,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.battery.stats import DailyBatteryTrend
    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.live_status.status import DeviceStatus
    from ecoflow_stats.outages.aggregates import OutageAggregates
    from ecoflow_stats.outages.model import Event, Gap
    from ecoflow_stats.outages.resolve import EffectiveOutage
    from ecoflow_stats.storage.devices import DeviceRecord


@dataclass(frozen=True, slots=True)
class DeviceOption:
    """One `<option>` in the named device selector (amendment item 10)."""

    id: int
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class OverviewViewModel:
    """Everything `overview.html` and `partials/live.html` render."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    has_data: bool
    stale: bool
    age_s: int | None
    grid: str
    soc: int | None


def device_label(record: DeviceRecord) -> str:
    """The masked-serial convention already used by `cli.py`'s
    `recompute` output and `bootstrap.build`'s notification device
    labels — a configured name is shown in full, but a bare serial is
    never shown past its last 4 characters on a page a LAN visitor
    without a password can already reach."""
    return record.name if record.name else f"…{record.sn[-4:]}"


def build_device_options(
    device_records: tuple[DeviceRecord, ...], *, selected_device_id: int
) -> tuple[DeviceOption, ...]:
    return tuple(
        DeviceOption(
            id=record.id, label=device_label(record), selected=record.id == selected_device_id
        )
        for record in device_records
    )


def build_overview_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    status: DeviceStatus,
) -> OverviewViewModel:
    """web-ui "Empty and Stale States Are Shown Explicitly": `has_data`
    is false only when the device has no recorded sample at all
    (`status.ts is None`), never inferred from a zero-valued reading."""
    return OverviewViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        has_data=status.ts is not None,
        stale=status.stale,
        age_s=status.age_s,
        grid=status.grid,
        soc=status.reading.soc if status.reading is not None else None,
    )


@dataclass(frozen=True, slots=True)
class OutagesSummary:
    """The outages page's headline numbers (task 15.5 scenario 1:
    "count, total, longest, mean, brief count, unknown time")."""

    count: int
    total_downtime_s: int
    longest_s: int | None
    mean_s: int | None
    """`None` when `count` is 0 -- never a fabricated 0-second mean."""
    brief_count: int
    unknown_time_s: int
    """Total duration of every recorded gap clipped to the range: time
    whose grid state is not directly known, the same "unknown" concept
    the mains strip hatches (design "Visual language")."""


@dataclass(frozen=True, slots=True)
class OutageEventRow:
    """One row of the events table (task 15.5 scenario 3: "each event's
    start/end, its uncertainty, and its source")."""

    start_ts: int
    end_ts: int | None
    start_uncertain: bool
    end_uncertain: bool
    source: str
    """`"detected"`, `"gap"`, or `"legacy"` (`resolve.EffectiveOutage.source`)."""


@dataclass(frozen=True, slots=True)
class HeatmapViewModel:
    """The weekday/hour distribution rendered server-side into the page
    (task instructions: consumed from `compute_aggregates`, not fetched
    client-side). Both arrays are marginal distributions, not a joint
    weekday-by-hour matrix -- `outages.aggregates.OutageAggregates` only
    ever computed the two independently (slice 23), so a true 2D
    cross-tabulation is not available data to render honestly."""

    hour_of_day: tuple[int, ...]
    day_of_week: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MainsStripSegment:
    """One run-length segment of the mains-strip fallback table."""

    start_ts: int
    end_ts: int
    state: str
    """`"present"`, `"absent"`, or `"unknown"`."""


@dataclass(frozen=True, slots=True)
class OutagesViewModel:
    """Everything `outages.html` renders."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    range_start: int
    range_end: int
    summary: OutagesSummary
    heatmap: HeatmapViewModel
    events: tuple[OutageEventRow, ...]
    gap_review_count: int
    mains_strip: tuple[MainsStripSegment, ...]
    mains_strip_src: str


def _clip(start: int, end: int | None, range_start: int, range_end: int) -> tuple[int, int] | None:
    """`[start, end)` clipped to `[range_start, range_end)`, or `None`
    when nothing of it falls inside the range. An open end (`end is
    None`, still ongoing) clips at `range_end`, never past it."""
    clipped_start = max(start, range_start)
    clipped_end = min(end if end is not None else range_end, range_end)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def _paint_segment(
    segments: list[tuple[int, int, str]], start: int, end: int, state: str
) -> list[tuple[int, int, str]]:
    """Overwrite `[start, end)` with `state`, splitting any segment it
    partially overlaps. A later paint wins over an earlier one -- the
    priority order `build_mains_strip_segments` relies on (same
    algorithm as `web.routes.api._build_mains_strip`; duplicated here,
    not imported, because that module is outside this slice's declared
    edit surface -- see apply-progress-batch13 design decision #5 for
    the "a future refactor could move this to views.py" note this
    slice acts on for the page's own fallback-table builder)."""
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


def _merge_adjacent_segments(segments: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for segment in segments:
        if merged and merged[-1][1] == segment[0] and merged[-1][2] == segment[2]:
            prev_start, _prev_end, state = merged[-1]
            merged[-1] = (prev_start, segment[1], state)
        else:
            merged.append(segment)
    return merged


def build_mains_strip_segments(
    *,
    outages: Sequence[EffectiveOutage],
    gaps: Sequence[Gap],
    range_start: int,
    range_end: int,
) -> tuple[MainsStripSegment, ...]:
    """A gapless present/absent/unknown run-length series covering the
    whole range (task 15.5 scenario 2 / the mains-strip fallback table).

    Painted lowest-priority first: the whole range starts `present`;
    every raw gap paints `unknown` over it; every outage (however
    sourced -- detected, a confirmed gap, or legacy) then paints
    `absent` over both, so a gap the user has since confirmed as a real
    outage renders as a solid cut, not hatched, even though the same
    window is also a `Gap` row in storage."""
    if range_end <= range_start:
        return ()
    segments: list[tuple[int, int, str]] = [(range_start, range_end, "present")]
    for gap in gaps:
        clipped = _clip(gap.start_ts, gap.end_ts, range_start, range_end)
        if clipped is not None:
            segments = _paint_segment(segments, clipped[0], clipped[1], "unknown")
    for outage in outages:
        clipped = _clip(outage.start_ts, outage.end_ts, range_start, range_end)
        if clipped is not None:
            segments = _paint_segment(segments, clipped[0], clipped[1], "absent")
    merged = _merge_adjacent_segments(segments)
    return tuple(
        MainsStripSegment(start_ts=start, end_ts=end, state=state) for start, end, state in merged
    )


def _sum_clipped_duration(entries: Sequence[Gap], range_start: int, range_end: int) -> int:
    total = 0
    for entry in entries:
        clipped = _clip(entry.start_ts, entry.end_ts, range_start, range_end)
        if clipped is not None:
            total += clipped[1] - clipped[0]
    return total


def build_outages_summary(
    *,
    aggregates: OutageAggregates,
    briefs: Sequence[EffectiveOutage],
    gaps: Sequence[Gap],
    range_start: int,
    range_end: int,
) -> OutagesSummary:
    mean_s = aggregates.total_downtime_s // aggregates.count if aggregates.count else None
    return OutagesSummary(
        count=aggregates.count,
        total_downtime_s=aggregates.total_downtime_s,
        longest_s=aggregates.longest_s,
        mean_s=mean_s,
        brief_count=len(briefs),
        unknown_time_s=_sum_clipped_duration(gaps, range_start, range_end),
    )


def build_outage_event_rows(outages: Sequence[EffectiveOutage]) -> tuple[OutageEventRow, ...]:
    """Most-recent-first (task 15.5 scenario 3) so a reviewer sees the
    latest events without scrolling."""
    ordered = sorted(outages, key=lambda outage: outage.start_ts, reverse=True)
    return tuple(
        OutageEventRow(
            start_ts=outage.start_ts,
            end_ts=outage.end_ts,
            start_uncertain=outage.start_uncertain,
            end_uncertain=outage.end_uncertain,
            source=outage.source,
        )
        for outage in ordered
    )


def format_local_dt(ts: int, tz: str) -> str:
    """Render an epoch-second timestamp as a locale-neutral, numeric
    local-time string (`YYYY-MM-DD HH:MM`) for the mains-strip fallback
    table -- numeric-only so it needs no i18n catalog entry, the same
    `ZoneInfo(tz)` conversion `outages.aggregates.compute_aggregates`
    already uses for its hour-of-day/day-of-week bins."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(ts, tz=UTC).astimezone(zone)
    return local.strftime("%Y-%m-%d %H:%M")


def build_outages_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    aggregates: OutageAggregates,
    outages: Sequence[EffectiveOutage],
    briefs: Sequence[EffectiveOutage],
    gaps: Sequence[Gap],
    gap_review_count: int,
    range_start: int,
    range_end: int,
    mains_strip_src: str,
) -> OutagesViewModel:
    return OutagesViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        range_start=range_start,
        range_end=range_end,
        summary=build_outages_summary(
            aggregates=aggregates,
            briefs=briefs,
            gaps=gaps,
            range_start=range_start,
            range_end=range_end,
        ),
        heatmap=HeatmapViewModel(
            hour_of_day=tuple(aggregates.hour_of_day), day_of_week=tuple(aggregates.day_of_week)
        ),
        events=build_outage_event_rows(outages),
        gap_review_count=gap_review_count,
        mains_strip=build_mains_strip_segments(
            outages=outages, gaps=gaps, range_start=range_start, range_end=range_end
        ),
        mains_strip_src=mains_strip_src,
    )


@dataclass(frozen=True, slots=True)
class BatteryChargePoint:
    """One point of the battery page's charge line (battery requirement
    "Charge History")."""

    ts: int
    soc: int | None


@dataclass(frozen=True, slots=True)
class BatteryDodRow:
    """One outage's row in the depth-of-discharge table (battery
    requirement "Depth of Discharge Per Outage")."""

    start_ts: int
    end_ts: int | None
    depth_of_discharge: int | None
    """`None` means "unavailable" (amendment item 6) -- the template
    renders this as the common unavailable label, never a fabricated
    `0`."""


@dataclass(frozen=True, slots=True)
class BatteryAutonomyRow:
    """One outage's row in the observed-autonomy table (battery
    requirement "Observed Autonomy Compared With Device Estimate")."""

    start_ts: int
    end_ts: int | None
    observed_h: float | None
    """`None` means the outage did not qualify at all -- too short, too
    shallow, still ongoing, or missing a boundary charge -- rendered as
    "not enough data", never a fabricated figure."""
    device_estimate_h: float | None
    """`None` only when `observed_h` is not `None` but the device
    itself reported no `dsg_remain_min_start` estimate at the outage's
    start (amendment item 7, "unavailable") -- a distinct, narrower
    case than `observed_h is None`, never conflated with it."""


@dataclass(frozen=True, slots=True)
class BatteryTrendRow:
    """One rollup day's row in the cycle/state-of-health trend table."""

    day: str
    cycles_last: int | None
    soh_last: float | None


@dataclass(frozen=True, slots=True)
class BatteryViewModel:
    """Everything `battery.html` renders."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    range_start: int
    range_end: int
    charge_series: tuple[BatteryChargePoint, ...]
    dod_rows: tuple[BatteryDodRow, ...]
    autonomy_rows: tuple[BatteryAutonomyRow, ...]
    trend_rows: tuple[BatteryTrendRow, ...]
    series_src: str
    trends_src: str
    current_soc: int | None
    """The "right now" snapshot the redesigned page's activity ring
    shows (presentation-only: the most recent already-fetched
    `charge_series` point's charge, never a new query) -- `None` when
    the range has no sample at all, rendered as "unavailable" rather
    than a fabricated 0%."""
    current_soh: float | None
    """Same snapshot derivation as `current_soc`, from the most recent
    already-fetched `trend_rows` day."""
    current_cycles: int | None
    """Same snapshot derivation as `current_soc`, from the most recent
    already-fetched `trend_rows` day."""


def build_battery_dod_row(event: Event) -> BatteryDodRow:
    """One outage's depth-of-discharge row, reusing
    `battery.stats.depth_of_discharge` directly rather than re-deriving
    its NULL-safety guard here."""
    dod = depth_of_discharge(event)
    return BatteryDodRow(
        start_ts=event.start_ts,
        end_ts=event.end_ts,
        depth_of_discharge=None if dod == "unavailable" else dod,
    )


_AUTONOMY_HOURS_DECIMALS = 1
"""Both hours columns are division results (SoC points / an hourly
discharge rate; minutes / 60) and essentially never land on a round
number -- displaying the raw float leaked binary noise like
`6.083333333333333h` instead of a clean `6.1h` (visual-QA batch fix01,
fix 2). Rounded here, once, for both columns, rather than in the i18n
template string -- `battery.autonomy.hours_value` stays a plain
`"{hours}h"` with no decimal spec, matching how every other formatted
number in this template set is already a plain value substitution."""


def build_battery_autonomy_row(event: Event) -> BatteryAutonomyRow:
    """One outage's observed-autonomy row, reusing
    `battery.service.observed_autonomy` directly -- the view layer
    only ever reshapes its result for the template, never re-derives
    the qualification or NULL-safety guards themselves."""
    result = observed_autonomy(event)
    if result == "not enough data":
        return BatteryAutonomyRow(
            start_ts=event.start_ts, end_ts=event.end_ts, observed_h=None, device_estimate_h=None
        )
    device_estimate_h = (
        None
        if result.device_estimate_h == "unavailable"
        else round(result.device_estimate_h, _AUTONOMY_HOURS_DECIMALS)
    )
    return BatteryAutonomyRow(
        start_ts=event.start_ts,
        end_ts=event.end_ts,
        observed_h=round(result.observed_h, _AUTONOMY_HOURS_DECIMALS),
        device_estimate_h=device_estimate_h,
    )


def build_battery_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    range_start: int,
    range_end: int,
    samples: Sequence[tuple[int, Reading]],
    outage_events: Sequence[Event],
    trend_days: Sequence[DailyBatteryTrend],
    series_src: str,
    trends_src: str,
) -> BatteryViewModel:
    """Build the battery page's view model from already-fetched data.

    `outage_events` ordered most-recent-first (matching
    `build_outage_event_rows`'s own convention) so both the
    depth-of-discharge and autonomy tables show the latest outage
    first without scrolling.
    """
    ordered_events = sorted(outage_events, key=lambda event: event.start_ts, reverse=True)
    charge_points = charge_history(samples)
    # DATA-04/UI-14: the fallback <details> table is bucketed/bounded
    # (qa-report-data-01.md/qa-report-ui-01.md) -- the chart's own live
    # data source (`series_src`) and `current_soc` below both keep
    # reading `charge_points` unbucketed; only this table-bound series
    # is reshaped.
    fallback_points = bucket_charge_history(
        charge_points, range_start=range_start, range_end=range_end
    )
    trend = battery_trend(trend_days)
    latest_trend_day = trend[-1] if trend else None
    return BatteryViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        range_start=range_start,
        range_end=range_end,
        charge_series=tuple(
            BatteryChargePoint(ts=point.ts, soc=point.soc) for point in fallback_points
        ),
        dod_rows=tuple(build_battery_dod_row(event) for event in ordered_events),
        autonomy_rows=tuple(build_battery_autonomy_row(event) for event in ordered_events),
        trend_rows=tuple(
            BatteryTrendRow(day=point.day, cycles_last=point.cycles_last, soh_last=point.soh_last)
            for point in trend
        ),
        series_src=series_src,
        trends_src=trends_src,
        current_soc=charge_points[-1].soc if charge_points else None,
        current_soh=latest_trend_day.soh_last if latest_trend_day is not None else None,
        current_cycles=latest_trend_day.cycles_last if latest_trend_day is not None else None,
    )


__all__ = [
    "BatteryAutonomyRow",
    "BatteryChargePoint",
    "BatteryDodRow",
    "BatteryTrendRow",
    "BatteryViewModel",
    "DeviceOption",
    "HeatmapViewModel",
    "MainsStripSegment",
    "OutageEventRow",
    "OutagesSummary",
    "OutagesViewModel",
    "OverviewViewModel",
    "build_battery_autonomy_row",
    "build_battery_dod_row",
    "build_battery_view_model",
    "build_device_options",
    "build_mains_strip_segments",
    "build_outage_event_rows",
    "build_outages_summary",
    "build_outages_view_model",
    "build_overview_view_model",
    "device_label",
    "format_local_dt",
]
