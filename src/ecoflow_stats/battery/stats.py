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


def soc_history(points: Sequence[tuple[int, int | None]]) -> list[ChargePoint]:
    """Same ordering contract as `charge_history`, for a caller that
    already queried only ``(ts, soc)`` pairs rather than full
    `Reading`s (UI2-05, qa-report-ui-02.md: see
    `storage.samples.SampleStore.soc_between`'s own docstring for the
    full root-cause measurement). Still pure core: takes exactly the
    plain tuples its caller already has, no `Reading` import needed
    here at all.

    Sorts defensively by ``ts``, matching `charge_history`, so an
    out-of-order caller still gets a correctly ordered series.
    """
    return [ChargePoint(ts=ts, soc=soc) for ts, soc in sorted(points, key=lambda pair: pair[0])]


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


@dataclass(frozen=True, slots=True)
class BatteryPowerStatus:
    """Battery power context for "right now" (UI-02, qa-report-ui-01.md:
    the overview and battery pages showed the charge percentage twice
    -- once inside the activity ring, once in adjacent text, using the
    exact same figure). The adjacent text now shows what the ring
    cannot: whether the battery is charging or discharging, its net
    power, and -- only when the device itself reports one -- an
    estimated time to full or empty.
    """

    direction: Literal["charging", "discharging", "idle"] | None
    """`None` when the device did not report enough to compute a net
    power at all (Named Defect "missing read as zero": never guessed
    from a one-sided reading)."""
    net_w: float | None
    """`batt_in_w - batt_out_w`. Positive while charging, negative while
    discharging, exactly `0.0` at idle -- `None` only when either side
    of the pair is itself `None`."""
    remaining_min: int | None
    """The device's own `chg_remain_min` while charging or
    `dsg_remain_min` while discharging -- never the other direction's
    estimate, and never fabricated when idle or unavailable."""


def battery_power_status(reading: Reading) -> BatteryPowerStatus:
    """Derive `reading`'s current charging/discharging direction, net
    battery power, and remaining-time estimate, purely from fields the
    device already reported on this one sample -- no new query, no
    smoothing, no estimation beyond what `reading` itself carries.
    """
    if reading.batt_in_w is None or reading.batt_out_w is None:
        return BatteryPowerStatus(direction=None, net_w=None, remaining_min=None)

    net_w = reading.batt_in_w - reading.batt_out_w
    if net_w > 0:
        return BatteryPowerStatus(
            direction="charging", net_w=net_w, remaining_min=reading.chg_remain_min
        )
    if net_w < 0:
        return BatteryPowerStatus(
            direction="discharging", net_w=net_w, remaining_min=reading.dsg_remain_min
        )
    return BatteryPowerStatus(direction="idle", net_w=net_w, remaining_min=None)


@dataclass(frozen=True, slots=True)
class BatteryTrendInsight:
    """A data-driven, one-sentence-ready summary of the retained rollup
    history's own first-to-last change (design critique,
    qa-report-ui-01.md: the trend chart had no insight caption at all,
    just a static aria-label). Every field is computed strictly from
    `battery_trend`'s own ordered days -- never invented, and `None`
    whenever there is not enough history to compare.
    """

    soh_delta: float | None
    """`last.soh_last - first.soh_last`, rounded to 1 decimal -- `None`
    when fewer than 2 days are available or either boundary's
    `soh_last` is itself missing."""
    soh_direction: Literal["up", "down", "steady"] | None
    """`None` exactly when `soh_delta` is `None`; `"steady"` only when
    the rounded delta is exactly `0.0`, never guessed."""
    cycles_delta: int | None
    """`last.cycles_last - first.cycles_last` -- `None` under the same
    conditions as `soh_delta`."""


def summarize_battery_trend(days: Sequence[DailyBatteryTrend]) -> BatteryTrendInsight:
    """Compare the retained rollup history's first and last day (by
    `battery_trend`'s own day-ordering) -- no new query, no smoothing,
    no estimation beyond what the two boundary days themselves report.
    """
    ordered = battery_trend(days)
    if len(ordered) < 2:
        return BatteryTrendInsight(soh_delta=None, soh_direction=None, cycles_delta=None)

    first, last = ordered[0], ordered[-1]

    soh_delta: float | None
    soh_direction: Literal["up", "down", "steady"] | None
    if first.soh_last is not None and last.soh_last is not None:
        soh_delta = round(last.soh_last - first.soh_last, 1)
        soh_direction = "steady" if soh_delta == 0 else ("up" if soh_delta > 0 else "down")
    else:
        soh_delta = None
        soh_direction = None

    cycles_delta = (
        last.cycles_last - first.cycles_last
        if first.cycles_last is not None and last.cycles_last is not None
        else None
    )
    return BatteryTrendInsight(
        soh_delta=soh_delta, soh_direction=soh_direction, cycles_delta=cycles_delta
    )


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
    "BatteryPowerStatus",
    "BatteryTrendInsight",
    "ChargePoint",
    "DailyBatteryTrend",
    "battery_power_status",
    "battery_trend",
    "bucket_charge_history",
    "charge_history",
    "depth_of_discharge",
    "soc_history",
    "summarize_battery_trend",
]
