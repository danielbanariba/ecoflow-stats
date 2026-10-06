"""Tests for `battery.stats`: pure charge history, depth of discharge,
and cycle/state-of-health trend shaping.

Battery spec: "Charge History", "Depth of Discharge Per Outage", "Cycle
Count and State-of-Health Trends"; amendment item 6 (DoD "unavailable"
on a NULL boundary state of charge); design-data section 4.6.
"""

from __future__ import annotations

from ecoflow_stats.battery.stats import (
    ChargePoint,
    DailyBatteryTrend,
    battery_trend,
    bucket_charge_history,
    charge_history,
    depth_of_discharge,
)
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.model import Event


def _reading(soc: int | None) -> Reading:
    return Reading(soc=soc)


def test_charge_history_reflects_recorded_samples_in_order() -> None:
    """Scenario "Charge history reflects recorded samples in order": a
    defect here (for example, trusting an unsorted caller-supplied batch)
    would show the wrong state of charge at the wrong time on the
    battery page's charge line -- this test feeds samples out of `ts`
    order to prove the function sorts rather than assuming order."""
    samples = [(600, _reading(80)), (0, _reading(95)), (300, _reading(88))]

    history = charge_history(samples)

    assert history == [
        ChargePoint(ts=0, soc=95),
        ChargePoint(ts=300, soc=88),
        ChargePoint(ts=600, soc=80),
    ]


def test_bucket_charge_history_leaves_a_short_series_unbucketed() -> None:
    """Pass-1 (DATA-04/UI-14, qa-report-data-01.md/qa-report-ui-01.md):
    a range with few enough samples needs no reshaping at all --
    bucketing a short series would only lose precision for no
    row-count benefit."""
    points = [ChargePoint(ts=0, soc=90), ChargePoint(ts=60, soc=85)]

    result = bucket_charge_history(points, range_start=0, range_end=120, max_rows=200)

    assert result == points


def test_bucket_charge_history_caps_a_long_series_at_max_rows() -> None:
    """Pass-1 (DATA-04/UI-14): before this fix, the SoC fallback table
    rendered one row per raw sample regardless of range length --
    ~64,777 rows for this app's own full seeded history, ~10,080 for
    just 7 days. A series longer than `max_rows` must be downsampled
    to at most `max_rows` rows.

    Pass-2 target: reverting the `len(points) <= max_rows` early
    return to always return `list(points)` turns this red -- `len(
    result)` would be `1000`, not `<= 50`."""
    points = [ChargePoint(ts=ts, soc=50) for ts in range(0, 10_000, 10)]  # 1000 points

    result = bucket_charge_history(points, range_start=0, range_end=10_000, max_rows=50)

    assert len(result) <= 50


def test_bucket_charge_history_summarizes_the_whole_range_not_just_its_tail() -> None:
    """Pass-1 (DATA-04/UI-14): a naive fix that just truncated to the
    first `max_rows` samples (`points[:max_rows]`) would also shrink
    the row count, but silently drop every reading past the first
    slice -- the requirement is to summarize the whole range
    accurately, not just its start.

    Pass-2 target: replacing the bucketing body with
    `return points[:max_rows]` turns this red -- the last bucket's
    `ts` would stay near `0`, not near `9_000`."""
    points = [ChargePoint(ts=ts, soc=50) for ts in range(0, 10_000, 10)]  # 1000 points

    result = bucket_charge_history(points, range_start=0, range_end=10_000, max_rows=50)

    assert result[-1].ts >= 9_000


def test_bucket_charge_history_averages_present_readings_without_treating_a_missing_one_as_zero() -> (
    None
):
    """Pass-1 (DATA-04/UI-14): the project's standing NULL-discipline
    (Named Defect "Missing read as zero") applies to a bucket's average
    exactly like it does everywhere else in this codebase -- a reading
    with no recorded SoC must never pull a bucket's average toward `0`.

    Pass-2 target: computing the average over the bucket's full sample
    count (counting the missing reading as a `0`) instead of only its
    present readings turns this red -- the average would be `40`
    (`(80 + 0) / 2`), not `80`."""
    points = [ChargePoint(ts=0, soc=80), ChargePoint(ts=1, soc=None)]

    result = bucket_charge_history(points, range_start=0, range_end=100, max_rows=1)

    assert len(result) == 1
    assert result[0].soc == 80


def test_depth_of_discharge_is_start_minus_the_lowest_point() -> None:
    """Scenario "Depth of discharge is start minus end": a defect that
    used `soc_end` instead of `soc_min` would understate the depth of
    discharge whenever solar partially recharged the battery before the
    grid actually returned."""
    event = Event(start_ts=0, soc_start=90, soc_min=42, soc_end=55)

    assert depth_of_discharge(event) == 48


def test_depth_of_discharge_is_unavailable_when_the_starting_charge_is_missing() -> None:
    """Amendment item 6, scenario "Missing boundary charge is reported
    as unavailable, not assumed": a defect that fell back to `0` or
    treated a missing boundary as "no discharge" would silently
    fabricate data the device never reported."""
    event = Event(start_ts=0, soc_start=None, soc_min=42)

    assert depth_of_discharge(event) == "unavailable"


def test_depth_of_discharge_is_unavailable_when_the_lowest_point_is_missing() -> None:
    """The symmetric half of amendment item 6 -- proven independently
    rather than assuming the previous test's `if` branch already covers
    both operands of the NULL check."""
    event = Event(start_ts=0, soc_start=90, soc_min=None)

    assert depth_of_discharge(event) == "unavailable"


def test_battery_trend_reflects_the_devices_own_progression_in_day_order() -> None:
    """Scenario "Trend reflects the device's own reported progression":
    a defect that returned rollup days out of order would misdraw the
    cycle/SoH trend chart's x-axis even though every value was itself
    correct."""
    days = [
        DailyBatteryTrend(
            day="2026-10-02",
            cycles_last=11,
            soh_last=97.0,
            soc_min=40,
            soc_max=90,
            batt_temp_max=28.0,
        ),
        DailyBatteryTrend(
            day="2026-10-01",
            cycles_last=10,
            soh_last=98.0,
            soc_min=50,
            soc_max=95,
            batt_temp_max=26.0,
        ),
    ]

    trend = battery_trend(days)

    assert [point.day for point in trend] == ["2026-10-01", "2026-10-02"]
    assert [point.cycles_last for point in trend] == [10, 11]
    assert [point.soh_last for point in trend] == [98.0, 97.0]
