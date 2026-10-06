"""Outage aggregates: count, total downtime, longest single outage, and
the hour-of-day/day-of-week distribution of each outage's start time
-- computed from confirmed outage events only (outages requirement
"Outage Aggregates").

Pure: takes an already-reconciled list of `resolve.EffectiveOutage`
(confirmed outages only -- an unconfirmed suspected phantom or an
unresolved gap never reaches this module at all, because `resolve()`
already excluded them) and a range to clip against. Depends only on
`outages.resolve`'s value type and the stdlib, never on `storage`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.outages.resolve import EffectiveOutage


@dataclass(frozen=True, slots=True)
class OutageAggregates:
    """The reconciled totals for one device over one range."""

    count: int
    total_downtime_s: int
    longest_s: int | None
    hour_of_day: list[int] = field(default_factory=lambda: [0] * 24)
    day_of_week: list[int] = field(default_factory=lambda: [0] * 7)
    """Monday = index 0 .. Sunday = index 6, matching `datetime.weekday()`."""


def compute_aggregates(
    *,
    outages: Sequence[EffectiveOutage],
    range_start: int,
    range_end: int,
    tz: str = "UTC",
) -> OutageAggregates:
    """Compute range-clipped totals and the start-time distribution.

    An ongoing outage (`end_ts is None`) is clipped at `range_end`, never
    treated as having already ended; an outage entirely outside
    `[range_start, range_end]` contributes nothing. The hour-of-day and
    day-of-week bins use each outage's own (unclipped) start time,
    converted to the local calendar in `tz` -- clipping only affects the
    downtime totals, never which bucket a start time falls into.
    """
    zone = ZoneInfo(tz)
    count = 0
    total = 0
    longest: int | None = None
    hour_of_day = [0] * 24
    day_of_week = [0] * 7

    for outage in outages:
        clipped_start = max(outage.start_ts, range_start)
        clipped_end = min(outage.end_ts if outage.end_ts is not None else range_end, range_end)
        if clipped_end <= clipped_start:
            continue

        duration = clipped_end - clipped_start
        count += 1
        total += duration
        longest = duration if longest is None else max(longest, duration)

        local_start = datetime.fromtimestamp(outage.start_ts, tz=UTC).astimezone(zone)
        hour_of_day[local_start.hour] += 1
        day_of_week[local_start.weekday()] += 1

    return OutageAggregates(
        count=count,
        total_downtime_s=total,
        longest_s=longest,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
    )


__all__ = ["OutageAggregates", "compute_aggregates"]
