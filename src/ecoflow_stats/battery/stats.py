"""Pure battery-stats core: charge history, per-outage depth of
discharge, and cycle/state-of-health trend shaping (battery spec:
"Charge History", "Depth of Discharge Per Outage", "Cycle Count and
State-of-Health Trends"; design-data section 4.6).

Pure domain logic, no I/O: `battery` is one of the packages
`tests/contract/test_pure_core_imports.py` scans for a forbidden
`storage`/`sqlite3`/... import (Named Defect "Core doing I/O"). This
module only ever shapes data its caller already queried -- the
orchestration layer that queries `storage` through `ports.py` and hands
data in here is `rollups.service` (for the per-day aggregation a
rollup refresh writes) and a later `battery.service` (Phase 17 PR ii,
not built in this slice).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.outages.model import Event


@dataclass(frozen=True, slots=True)
class ChargePoint:
    """One charge-history point: a sample's timestamp and state of charge."""

    ts: int
    soc: int | None


@dataclass(frozen=True, slots=True)
class DailyBatteryTrend:
    """One rollup day's battery-trend fields, exactly as
    `rollups.service.derive_rollups` already computed and stored them --
    `battery_trend` only orders them into a series, it never recomputes
    anything (design-data section 4.6: "Trends: ... per day from
    rollups")."""

    day: str
    cycles_last: int | None
    soh_last: float | None
    soc_min: int | None
    soc_max: int | None
    batt_temp_max: float | None


def charge_history(samples: Sequence[tuple[int, Reading]]) -> list[ChargePoint]:
    """The state-of-charge series for a requested period, in timestamp
    order (battery requirement "Charge History", scenario "Charge
    history reflects recorded samples in order").

    Sorts defensively by ``ts`` rather than trusting the caller's query
    order, so a caller that hands in an unordered batch still gets a
    correctly ordered series.
    """
    return [
        ChargePoint(ts=ts, soc=reading.soc)
        for ts, reading in sorted(samples, key=lambda pair: pair[0])
    ]


_DEFAULT_MAX_FALLBACK_ROWS = 200


def bucket_charge_history(
    points: Sequence[ChargePoint],
    *,
    range_start: int,
    range_end: int,
    max_rows: int = _DEFAULT_MAX_FALLBACK_ROWS,
) -> list[ChargePoint]:
    """Downsample `points` (`charge_history`'s own one-row-per-sample
    series) to at most `max_rows` points spanning `[range_start,
    range_end)`, for the SoC chart's `<details>` accessibility fallback
    table (DATA-04/UI-14, qa-report-data-01.md/qa-report-ui-01.md): a
    full history's fallback table used to render one row per raw
    sample -- ~64,777 rows / 6.8MB for this app's own seeded history,
    ~10,080 for just a 7-day range -- even though the chart itself
    (`/api/v1/battery/series`) already renders that many points fine;
    a server-rendered `<table>` row has a per-row DOM/accessibility
    cost a `<canvas>`/SVG chart point does not.

    Mirrors the bucketing *concept* `web.routes.api._select_bucket`
    already applies to the grid-voltage chart's own live data (coarser
    buckets for a longer range), reimplemented here rather than
    imported: `battery` is pure core (`tests/contract.
    test_pure_core_imports` scans it for a forbidden `web`/`storage`
    import), so it cannot import anything from `web.routes.api`.

    A bucket's `soc` is the rounded average of its own present
    (non-`None`) readings only, never pulled toward `0` by a missing
    one (the project's standing NULL-discipline, Named Defect "Missing
    read as zero") -- a bucket with no present reading at all stays
    `None`. The chart's own live data source is completely unaffected;
    this only reshapes the fallback table.
    """
    if range_end <= range_start or len(points) <= max_rows:
        return list(points)
    bucket_width_s = -(-(range_end - range_start) // max_rows)  # ceiling division
    socs_by_bucket: dict[int, list[int]] = {}
    bucket_order: list[int] = []
    for point in points:
        bucket_start = range_start + (point.ts - range_start) // bucket_width_s * bucket_width_s
        if bucket_start not in socs_by_bucket:
            socs_by_bucket[bucket_start] = []
            bucket_order.append(bucket_start)
        if point.soc is not None:
            socs_by_bucket[bucket_start].append(point.soc)
    return [
        ChargePoint(
            ts=bucket_start,
            soc=round(sum(socs) / len(socs)) if (socs := socs_by_bucket[bucket_start]) else None,
        )
        for bucket_start in bucket_order
    ]


def depth_of_discharge(event: Event) -> int | Literal["unavailable"]:
    """Depth of discharge for one outage event: the starting charge
    minus the deepest point of discharge reached during it (battery
    requirement "Depth of Discharge Per Outage"; design-data section
    4.6: `soc_start - soc_min`).

    `soc_min` -- the lowest state of charge recorded during the event
    -- is used rather than `soc_end` (the charge recorded when the grid
    returned) because `soc_min` already accounts for any solar recharge
    that happened mid-outage, matching the engineering definition of
    "how deep the discharge went"; `soc_end` would understate it.

    Reports `"unavailable"` -- never estimated, never a fabricated `0`,
    and never conflated with "zero discharge" -- whenever either
    boundary charge is unknown (amendment item 6; scenario "Missing
    boundary charge is reported as unavailable, not assumed").
    """
    if event.soc_start is None or event.soc_min is None:
        return "unavailable"
    return event.soc_start - event.soc_min


def battery_trend(days: Sequence[DailyBatteryTrend]) -> list[DailyBatteryTrend]:
    """The cycle-count and state-of-health trend across the retained
    rollup history, in day order (battery requirement "Cycle Count and
    State-of-Health Trends", scenario "Trend reflects the device's own
    reported progression").

    Sorts defensively by ``day`` for the same reason `charge_history`
    sorts by ``ts``: two rollup rows for the same device never share a
    day (storage primary key), so this never discards or merges a row,
    only orders what the caller already fetched.
    """
    return sorted(days, key=lambda point: point.day)


__all__ = [
    "ChargePoint",
    "DailyBatteryTrend",
    "battery_trend",
    "bucket_charge_history",
    "charge_history",
    "depth_of_discharge",
]
