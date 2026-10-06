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

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.live_status.status import DeviceStatus
    from ecoflow_stats.outages.aggregates import OutageAggregates
    from ecoflow_stats.outages.model import Gap
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


__all__ = [
    "DeviceOption",
    "HeatmapViewModel",
    "MainsStripSegment",
    "OutageEventRow",
    "OutagesSummary",
    "OutagesViewModel",
    "OverviewViewModel",
    "build_device_options",
    "build_mains_strip_segments",
    "build_outage_event_rows",
    "build_outages_summary",
    "build_outages_view_model",
    "build_overview_view_model",
    "device_label",
    "format_local_dt",
]
