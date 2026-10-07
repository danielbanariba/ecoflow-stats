"""Pure view-model builders for the web pages.

Kept separate from route handlers so "what does this page show" is
testable without FastAPI, Jinja2, or storage: every function here takes
already-fetched data (a `DeviceStatus`, device records) and returns
plain dataclasses the templates render directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from ecoflow_stats.battery.service import observed_autonomy
from ecoflow_stats.battery.stats import (
    BatteryPowerStatus,
    BatteryTrendInsight,
    battery_power_status,
    battery_trend,
    bucket_charge_history,
    depth_of_discharge,
    soc_history,
    summarize_battery_trend,
)
from ecoflow_stats.timeutil import duration_parts, local_day, period_end_ts, period_start_ts

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ecoflow_stats.battery.stats import DailyBatteryTrend
    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.live_status.status import DeviceStatus
    from ecoflow_stats.outages.aggregates import OutageAggregates
    from ecoflow_stats.outages.model import Event, Gap
    from ecoflow_stats.outages.resolve import EffectiveOutage
    from ecoflow_stats.storage.devices import DeviceRecord
    from ecoflow_stats.storage.rollups import DailyGridRollup


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
    last_update_ts: int | None
    """The latest sample's own timestamp (UI-04/UI-05, qa-report-ui-01.md)
    -- `None` only when `has_data` is `False`, never a fabricated "now"."""
    last_update_is_today: bool
    """Whether `last_update_ts` falls on the viewer's configured local
    calendar day (C-02, qa-report-ui-01.md) -- lets the template show a
    bare `"HH:MM"` for the common case instead of always spelling out a
    full date that is almost always today anyway. `False` when there is
    no sample at all."""
    solar_in_w: float | None
    ac_in_w: float | None
    ac_out_w: float | None
    """Current power flows from the latest sample (UI-04), straight off
    `Reading` -- `None` means the device did not report that field,
    never a fabricated `0`."""
    battery_power: BatteryPowerStatus | None
    """`None` only when there is no reading at all; see
    `battery.stats.battery_power_status` for why a one-sided reading
    also yields a `None` direction/net inside this value."""
    soh: float | None
    cycles: int | None
    """Battery health straight off the latest sample (UI-04) -- not the
    range-aggregated rollup trend `battery.html` shows, since the
    overview has no date range to aggregate over."""
    outages_30d: OutagesSummary
    """The same `OutagesSummary` shape the outages page's own summary
    uses, computed by the caller over a fixed trailing 30 days (UI-05)
    -- never recomputed here, only reshaped alongside everything else."""


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
    outages_30d: OutagesSummary,
    tz: str,
    now_ts: int,
) -> OverviewViewModel:
    """web-ui "Empty and Stale States Are Shown Explicitly": `has_data`
    is false only when the device has no recorded sample at all
    (`status.ts is None`), never inferred from a zero-valued reading.

    `outages_30d` is already computed by the caller (the same
    `outages.aggregates`/`resolve` pipeline `outages_page` uses, over a
    fixed trailing 30-day window) -- this function only reshapes
    already-fetched data, it never queries storage on its own.

    `tz`/`now_ts` are only used to decide `last_update_is_today` (C-02)
    -- compared as local calendar days under `tz`, never as raw epoch
    proximity, so a reading from early this local morning still counts
    as "today" even hours after it was taken.
    """
    reading = status.reading
    return OverviewViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        has_data=status.ts is not None,
        stale=status.stale,
        age_s=status.age_s,
        grid=status.grid,
        soc=reading.soc if reading is not None else None,
        last_update_ts=status.ts,
        last_update_is_today=(
            status.ts is not None and local_day(status.ts, tz) == local_day(now_ts, tz)
        ),
        solar_in_w=reading.solar_in_w if reading is not None else None,
        ac_in_w=reading.ac_in_w if reading is not None else None,
        ac_out_w=reading.ac_out_w if reading is not None else None,
        battery_power=battery_power_status(reading) if reading is not None else None,
        soh=reading.soh if reading is not None else None,
        cycles=reading.cycles if reading is not None else None,
        outages_30d=outages_30d,
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
    """The joint weekday x hour distribution rendered server-side into
    the page (task instructions: consumed from `compute_aggregates`,
    not fetched client-side). `matrix[weekday][hour]` -- Monday = index
    0 .. Sunday = index 6, hour local 0..23 (DATA-03, qa-report-data-
    01.md: replaces the two independent 1D marginals this view model
    used to carry, which could never show a real pattern like "always
    on Monday afternoons", only that outages happen on Mondays
    sometimes and at 14:00 sometimes)."""

    matrix: tuple[tuple[int, ...], ...]


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
    legacy_review_count: int
    """How many imported legacy outages are suspected phantoms still
    awaiting a human verdict (UI-11, qa-report-ui-01.md) -- the same
    role `gap_review_count` plays for gaps, surfaced so the page's
    legacy-review section can show a count before the list is
    expanded."""
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


_DURATION_UNIT_KEYS = {
    "days": "duration.unit.days",
    "hours": "duration.unit.hours",
    "minutes": "duration.unit.minutes",
    "seconds": "duration.unit.seconds",
}


def format_duration(seconds: int, t: Callable[..., str]) -> str:
    """Render a duration in seconds as a short, human-readable string
    (`"7 h"`, `"1 h 25 min"`, `"2 d 3 h"`) instead of a raw second
    count like `"25200s"` (C-01, qa-report-ui-01.md) -- the overview's
    "Longest outage"/"Time on battery" tiles and the outages page's
    summary `<dl>` all render a duration through this, over
    `timeutil.duration_parts`'s cascaded units."""
    return " ".join(
        t(_DURATION_UNIT_KEYS[name]).format(count=value) for name, value in duration_parts(seconds)
    )


def format_hours_duration(hours: float, t: Callable[..., str]) -> str:
    """Render a duration given in (fractional) hours through the exact
    same `format_duration` every other duration on this app already
    uses, instead of this page's own `"{hours}h"` plain substitution
    (C-01 followup, qa-report-ui-02.md: the battery page's observed-
    autonomy table was the one place left rendering a duration as a
    decimal-hours value like `"14.0h"` while everywhere else -- the
    overview's "Longest outage"/"Time on battery" tiles, the outages
    page's summary -- already showed `"12 h 31 min"`. Chart axes are
    deliberately left alone: a tick label's own compact scale is a
    different, legitimate concern from a stat value's readability).

    `build_battery_autonomy_row` already rounds `hours` to one decimal
    before this ever runs, so the `round()` below only guards against
    an exact `.5`-second float artifact from that rounding itself
    (for example `6.1 * 3600`), never the raw unrounded ratio."""
    return format_duration(round(hours * 3600), t)


def format_compact_local_dt(ts: int, tz: str, show_date: bool) -> str:
    """Render an epoch-second timestamp as a compact local time for the
    overview's "Last update" tile (C-02, qa-report-ui-01.md): just
    `"HH:MM"` when `show_date` is `False` (the sample is from today's
    local calendar day), or `"MM-DD HH:MM"` when it is not -- never the
    full `"YYYY-MM-DD HH:MM"` `format_local_dt` renders for the
    mains-strip fallback table, which dwarfed this tile with a date
    that is almost always today anyway."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(ts, tz=UTC).astimezone(zone)
    return local.strftime("%m-%d %H:%M") if show_date else local.strftime("%H:%M")


_MONTH_ABBR_KEYS = tuple(f"date.month_abbr.{month}" for month in range(1, 13))


def format_period_label(period: str, granularity: str, t: Callable[..., str]) -> str:
    """Render an energy period string -- `"YYYY-MM-DD"` (daily) or
    `"YYYY-MM"` (monthly) -- as a short, locale-appropriate date (F2,
    orchestrator QA batch F: the summary cards showed the raw ISO
    string truncated mid-digit, e.g. `"2026-10-..."`, instead of a
    real readable date) through the same `t()` translator catalog
    lookup every other piece of UI copy on this page already goes
    through, rather than a hardcoded English month name."""
    if granularity == "monthly":
        year, month = period.split("-")
        return t("energy.period.month_year").format(
            month=t(_MONTH_ABBR_KEYS[int(month) - 1]), year=year
        )
    year, month, day = period.split("-")
    return t("energy.period.day_month_year").format(
        day=int(day), month=t(_MONTH_ABBR_KEYS[int(month) - 1]), year=year
    )


def build_outages_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    aggregates: OutageAggregates,
    outages: Sequence[EffectiveOutage],
    briefs: Sequence[EffectiveOutage],
    gaps: Sequence[Gap],
    gap_review_count: int,
    legacy_review_count: int,
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
        heatmap=HeatmapViewModel(matrix=tuple(tuple(row) for row in aggregates.heatmap)),
        events=build_outage_event_rows(outages),
        gap_review_count=gap_review_count,
        legacy_review_count=legacy_review_count,
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
    battery_power: BatteryPowerStatus | None
    """UI-02 (qa-report-ui-01.md): the same charging/discharging/net-
    watts/remaining-time context the overview's hero now shows, derived
    from the exact same latest-by-`ts` already-fetched `samples` entry
    `current_soc` itself comes from -- `None` only when the range has
    no sample at all."""
    trend_insight: BatteryTrendInsight
    """Design critique (qa-report-ui-01.md): a data-driven, one-sentence
    caption for the cycle/SoH trend chart, computed from the same
    already-fetched `trend_rows` -- never a static placeholder."""


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
fix 2). Rounded here, once, for both columns, before `format_hours_
duration` (C-01 followup, qa-report-ui-02.md) turns that one decimal
into the same `"6 h 6 min"` style every other duration on this app
renders -- rounding first keeps the two independent concerns (how
many decimals a ratio deserves, and how a duration should read)
separate."""


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
    soc_samples: Sequence[tuple[int, int | None]],
    latest_reading: Reading | None,
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

    UI2-05 (qa-report-ui-02.md): takes `soc_samples` (lean ``(ts,
    soc)`` pairs) and the range's own `latest_reading` separately,
    rather than one `Sequence[tuple[int, Reading]]` of every sample --
    this function only ever reads `.soc` from the series and a full
    `Reading` from the single latest sample, so the caller
    (`web.routes.pages.battery_page`) can fetch exactly that lean
    shape from storage instead of building a full `Reading` per row
    for a history that can run into the tens of thousands of samples.
    """
    ordered_events = sorted(outage_events, key=lambda event: event.start_ts, reverse=True)
    charge_points = soc_history(soc_samples)
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
        battery_power=battery_power_status(latest_reading) if latest_reading is not None else None,
        trend_insight=summarize_battery_trend(trend_days),
    )


@dataclass(frozen=True, slots=True)
class EnergyPeriodRow:
    """One row of the energy page's periods table (energy requirements
    "Daily Energy by Source and Direction" / "Energy Cost from a
    Configurable Flat Tariff"; task 19.1's page half) -- the exact same
    period dict `web.routes.api.build_energy_periods` already computes,
    reshaped so the template addresses it by attribute instead of by
    dict key. Never a second copy of that function's cost/flag
    computation, only a reshape of its already-computed output."""

    period: str
    """`"YYYY-MM-DD"` at daily granularity, `"YYYY-MM"` at monthly."""
    chg_ac_wh: float
    chg_dc_wh: float
    chg_solar_wh: float
    dsg_ac_wh: float
    dsg_dc_wh: float
    chg_ac_est_wh: float
    cost: float | Literal["unavailable"]
    """`"unavailable"` only when no tariff is configured -- never a
    fabricated `0` (energy requirement "Energy Cost from a
    Configurable Flat Tariff")."""
    currency: str
    flags: tuple[str, ...]
    """Decoded flag names (`"counter_reset"`, `"implausible_jump"`,
    `"gap_prorated"`) -- empty when the period is a clean estimate."""


@dataclass(frozen=True, slots=True)
class EnergyViewModel:
    """Everything `energy.html` renders."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    range_start: int
    range_end: int
    granularity: str
    """`"daily"` or `"monthly"` -- the same `select_bucket` decision
    `energy_daily_route` applies to its chart's own data source, so the
    page's table and its chart always agree."""
    periods: tuple[EnergyPeriodRow, ...]
    total_in_wh: float
    total_out_wh: float
    total_cost: float | Literal["unavailable"]
    """`"unavailable"` whenever `tariff` was `None` -- never a sum over
    each period's own `"unavailable"` sentinel mistaken for `0`."""
    currency: str
    best_day: EnergyPeriodRow | None
    """The period with the lowest cost -- `None` when no tariff is
    configured or there are no periods at all, never ranked by a
    substitute metric (apply-progress-pages design decision)."""
    worst_day: EnergyPeriodRow | None
    """The period with the highest cost, same `None` conditions as
    `best_day`."""
    series_src: str


def build_energy_period_row(period: dict[str, object]) -> EnergyPeriodRow:
    """Adapts one of `web.routes.api.build_energy_periods`'s period
    dicts into a dataclass."""
    return EnergyPeriodRow(
        period=period["period"],  # type: ignore[arg-type]
        chg_ac_wh=period["chg_ac_wh"],  # type: ignore[arg-type]
        chg_dc_wh=period["chg_dc_wh"],  # type: ignore[arg-type]
        chg_solar_wh=period["chg_solar_wh"],  # type: ignore[arg-type]
        dsg_ac_wh=period["dsg_ac_wh"],  # type: ignore[arg-type]
        dsg_dc_wh=period["dsg_dc_wh"],  # type: ignore[arg-type]
        chg_ac_est_wh=period["chg_ac_est_wh"],  # type: ignore[arg-type]
        cost=period["cost"],  # type: ignore[arg-type]
        currency=period["currency"],  # type: ignore[arg-type]
        flags=tuple(period["flags"]),  # type: ignore[arg-type]
    )


def build_energy_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    range_start: int,
    range_end: int,
    granularity: str,
    periods: Sequence[dict[str, object]],
    tariff: float | None,
    currency: str,
    series_src: str,
    now_ts: int,
    tz: str,
    history_start_ts: int | None,
) -> EnergyViewModel:
    """Build the energy page's view model from
    `web.routes.api.build_energy_periods`'s already-computed period
    dicts -- `tariff` (not an inference from the rows) decides
    `total_cost`/`best_day`/`worst_day`'s availability, since every
    period shares the one app-wide tariff and can never disagree with
    each other about whether a cost was computed at all.

    `now_ts`/`tz`/`history_start_ts` decide `best_day`/`worst_day`'s
    eligibility, never `total_in_wh`/`total_out_wh`/`total_cost` (F2,
    orchestrator QA batch F, extended to the symmetric start-of-
    history case): the current local day's (or month's) own in-
    progress period, and a device's partial first day/month of
    recorded history, both still contribute honestly to the range's
    totals -- they are only excluded from the "cheapest"/"most
    expensive" ranking, which a period with just a few hours of real
    data would otherwise win or lose unfairly against one that ran
    its full length. A period is eligible only when it has both ended
    (`now_ts`) and started no earlier than the device's observed
    history (`history_start_ts`); `history_start_ts=None` (no recorded
    sample at all) makes every period ineligible. A range with no
    complete period at all reports `None` for both, same as no tariff
    being configured -- never a ranking over an incomplete field."""
    rows = tuple(build_energy_period_row(period) for period in periods)
    total_in_wh = sum(row.chg_ac_wh + row.chg_dc_wh + row.chg_solar_wh for row in rows)
    total_out_wh = sum(row.dsg_ac_wh + row.dsg_dc_wh for row in rows)
    complete_rows = tuple(
        row
        for row in rows
        if period_end_ts(row.period, granularity, tz) <= now_ts
        and history_start_ts is not None
        and history_start_ts <= period_start_ts(row.period, granularity, tz)
    )
    best_day: EnergyPeriodRow | None = None
    worst_day: EnergyPeriodRow | None = None
    total_cost: float | Literal["unavailable"]
    if tariff is None:
        total_cost = "unavailable"
    else:
        total_cost = sum(row.cost for row in rows)  # type: ignore[misc]
        if complete_rows:
            best_day = min(complete_rows, key=lambda row: row.cost)  # type: ignore[arg-type,return-value]
            worst_day = max(complete_rows, key=lambda row: row.cost)  # type: ignore[arg-type,return-value]
    return EnergyViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        range_start=range_start,
        range_end=range_end,
        granularity=granularity,
        periods=rows,
        total_in_wh=total_in_wh,
        total_out_wh=total_out_wh,
        total_cost=total_cost,
        currency=currency,
        best_day=best_day,
        worst_day=worst_day,
        series_src=series_src,
    )


@dataclass(frozen=True, slots=True)
class GridDailyRow:
    """One day's row in the grid page's daily ranges table
    (grid-quality requirement "Daily Voltage and Frequency Ranges") --
    the same `storage.rollups.DailyGridRollup` shape, reshaped so the
    template only ever addresses view-model attributes, never a
    storage dataclass directly."""

    day: str
    grid_v_min: float | None
    grid_v_avg: float | None
    grid_v_max: float | None
    grid_hz_min: float | None
    grid_hz_avg: float | None
    grid_hz_max: float | None
    readings: int
    """`0` on a day with no grid-present judged reading at all -- the
    template shows that day as unavailable, never a fabricated 0V/0Hz
    range."""


@dataclass(frozen=True, slots=True)
class GridViewModel:
    """Everything `grid.html` renders."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    range_start: int
    range_end: int
    daily_rows: tuple[GridDailyRow, ...]
    threshold_v: float
    """The configured outage-detector voltage floor
    (`DetectorConfig.threshold_v`) the page's plain-language
    explanation refers to -- grid power reads as absent at or below
    this voltage."""
    typical_v: float | None
    """Average of every present day's own `grid_v_avg` -- `None` only
    when no day in the range had any grid-present reading at all."""
    lowest_v: float | None
    highest_v: float | None
    lowest_hz: float | None
    """`None` when no present day also reported a frequency range --
    `grid.quality.grid_quality_range`'s own documented asymmetry: a
    present reading can still lack frequency, so this is computed over
    a narrower set of days than `lowest_v`/`highest_v`, never the same
    present-day filter reused for both."""
    highest_hz: float | None
    days_with_data: int
    days_analyzed: int
    """The count of rollup rows read for this range -- not an actual
    calendar-day span -- labelled plainly on the page as "days
    analyzed" rather than claiming calendar-day coverage this never
    computed."""
    series_src: str


def build_grid_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    range_start: int,
    range_end: int,
    rows: Sequence[DailyGridRollup],
    threshold_v: float,
    series_src: str,
) -> GridViewModel:
    daily_rows = tuple(
        GridDailyRow(
            day=row.day,
            grid_v_min=row.grid_v_min,
            grid_v_avg=row.grid_v_avg,
            grid_v_max=row.grid_v_max,
            grid_hz_min=row.grid_hz_min,
            grid_hz_avg=row.grid_hz_avg,
            grid_hz_max=row.grid_hz_max,
            readings=row.grid_readings,
        )
        for row in rows
    )
    present_rows = tuple(row for row in daily_rows if row.grid_v_avg is not None)
    freq_rows = tuple(row for row in present_rows if row.grid_hz_min is not None)
    typical_v = (
        sum(row.grid_v_avg for row in present_rows) / len(present_rows)  # type: ignore[arg-type]
        if present_rows
        else None
    )
    lowest_v = min((row.grid_v_min for row in present_rows), default=None)  # type: ignore[type-var]
    highest_v = max((row.grid_v_max for row in present_rows), default=None)  # type: ignore[type-var]
    lowest_hz = min((row.grid_hz_min for row in freq_rows), default=None)  # type: ignore[type-var]
    highest_hz = max((row.grid_hz_max for row in freq_rows), default=None)  # type: ignore[type-var]
    return GridViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        range_start=range_start,
        range_end=range_end,
        daily_rows=daily_rows,
        threshold_v=threshold_v,
        typical_v=typical_v,
        lowest_v=lowest_v,
        highest_v=highest_v,
        lowest_hz=lowest_hz,
        highest_hz=highest_hz,
        days_with_data=len(present_rows),
        days_analyzed=len(daily_rows),
        series_src=series_src,
    )


__all__ = [
    "BatteryAutonomyRow",
    "BatteryChargePoint",
    "BatteryDodRow",
    "BatteryTrendRow",
    "BatteryViewModel",
    "DeviceOption",
    "EnergyPeriodRow",
    "EnergyViewModel",
    "GridDailyRow",
    "GridViewModel",
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
    "build_energy_period_row",
    "build_energy_view_model",
    "build_grid_view_model",
    "build_mains_strip_segments",
    "build_outage_event_rows",
    "build_outages_summary",
    "build_outages_view_model",
    "build_overview_view_model",
    "device_label",
    "format_local_dt",
    "format_period_label",
]
