"""Tests for `battery.stats`: pure charge history, depth of discharge,
and cycle/state-of-health trend shaping.

Battery spec: "Charge History", "Depth of Discharge Per Outage", "Cycle
Count and State-of-Health Trends"; amendment item 6 (DoD "unavailable"
on a NULL boundary state of charge); design-data section 4.6.
"""

from __future__ import annotations

from ecoflow_stats.battery.stats import (
    BatteryPowerStatus,
    BatteryTrendInsight,
    ChargePoint,
    DailyBatteryTrend,
    battery_power_status,
    battery_trend,
    bucket_charge_history,
    charge_history,
    depth_of_discharge,
    soc_history,
    summarize_battery_trend,
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


def test_soc_history_reflects_recorded_samples_in_order() -> None:
    """UI2-05 (qa-report-ui-02.md): `soc_history` is `charge_history`'s
    lean sibling for a caller that already queried only `(ts, soc)`
    pairs (`storage.samples.SampleStore.soc_between`) instead of full
    `Reading`s. Same Pass-1 as `charge_history`'s own test above: a
    defect here that trusted an unsorted caller-supplied batch would
    show the wrong state of charge at the wrong time."""
    points = [(600, 80), (0, 95), (300, None)]

    history = soc_history(points)

    assert history == [
        ChargePoint(ts=0, soc=95),
        ChargePoint(ts=300, soc=None),
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


def test_battery_power_status_reports_the_sign_of_net_power_as_a_direction() -> None:
    """UI-02 (qa-report-ui-01.md): the overview and battery pages showed
    the battery's charge percentage twice (the ring, and adjacent text)
    -- the adjacent text now shows what the ring cannot: whether the
    battery is charging or discharging right now. A defect that
    mislabeled the direction (e.g. always "charging", or comparing the
    wrong sign) would show the opposite of what is actually happening."""
    charging = battery_power_status(Reading(batt_in_w=100.0, batt_out_w=0.0, chg_remain_min=30))
    assert charging == BatteryPowerStatus(direction="charging", net_w=100.0, remaining_min=30)

    discharging = battery_power_status(Reading(batt_in_w=0.0, batt_out_w=250.0, dsg_remain_min=45))
    assert discharging == BatteryPowerStatus(
        direction="discharging", net_w=-250.0, remaining_min=45
    )

    idle = battery_power_status(Reading(batt_in_w=0.0, batt_out_w=0.0))
    assert idle == BatteryPowerStatus(direction="idle", net_w=0.0, remaining_min=None)


def test_battery_power_status_reports_unavailable_rather_than_a_fabricated_net_power() -> None:
    """Named Defect "missing read as zero": a defect that treated a
    missing `batt_in_w`/`batt_out_w` as `0` would fabricate a net
    reading (e.g. claim "idle") instead of admitting the device did not
    report enough to compute one -- even when only ONE side of the pair
    is missing, the other side alone cannot honestly produce a net."""
    both_missing = battery_power_status(Reading(batt_in_w=None, batt_out_w=None))
    assert both_missing == BatteryPowerStatus(direction=None, net_w=None, remaining_min=None)

    one_sided = battery_power_status(Reading(batt_in_w=100.0, batt_out_w=None))
    assert one_sided == BatteryPowerStatus(direction=None, net_w=None, remaining_min=None)


def test_battery_power_status_omits_remaining_time_the_device_does_not_report() -> None:
    """UI-02's "time to full/empty when the device reports it" is
    conditional: a defect that fabricated a remaining-time guess, or
    read the wrong field for the current direction (e.g. showing
    `dsg_remain_min` while charging), would show false precision or the
    wrong estimate next to the ring."""
    charging_no_estimate = battery_power_status(
        Reading(batt_in_w=100.0, batt_out_w=0.0, chg_remain_min=None, dsg_remain_min=99)
    )
    assert charging_no_estimate.remaining_min is None

    discharging_uses_its_own_field = battery_power_status(
        Reading(batt_in_w=0.0, batt_out_w=50.0, chg_remain_min=15, dsg_remain_min=120)
    )
    assert discharging_uses_its_own_field.remaining_min == 120


def test_summarize_battery_trend_reports_the_change_from_the_first_to_the_last_day() -> None:
    """Design critique (qa-report-ui-01.md): the battery trend chart had
    no insight caption at all -- just a static aria-label. A defect
    that compared the wrong two days (e.g. the two most recent instead
    of first-vs-last), or got the direction backwards, would show a
    sentence that contradicts the chart sitting right next to it."""
    days = [
        DailyBatteryTrend(
            day="2026-01-01",
            cycles_last=10,
            soh_last=99.0,
            soc_min=20,
            soc_max=95,
            batt_temp_max=25.0,
        ),
        DailyBatteryTrend(
            day="2026-01-02",
            cycles_last=12,
            soh_last=98.5,
            soc_min=15,
            soc_max=90,
            batt_temp_max=26.0,
        ),
        DailyBatteryTrend(
            day="2026-01-07",
            cycles_last=25,
            soh_last=97.0,
            soc_min=10,
            soc_max=88,
            batt_temp_max=27.0,
        ),
    ]

    insight = summarize_battery_trend(days)

    assert insight == BatteryTrendInsight(soh_delta=-2.0, soh_direction="down", cycles_delta=15)


def test_summarize_battery_trend_reports_steady_when_soh_is_unchanged() -> None:
    days = [
        DailyBatteryTrend(
            day="2026-01-01",
            cycles_last=10,
            soh_last=98.0,
            soc_min=20,
            soc_max=95,
            batt_temp_max=25.0,
        ),
        DailyBatteryTrend(
            day="2026-01-07",
            cycles_last=10,
            soh_last=98.0,
            soc_min=20,
            soc_max=95,
            batt_temp_max=25.0,
        ),
    ]

    insight = summarize_battery_trend(days)

    assert insight == BatteryTrendInsight(soh_delta=0.0, soh_direction="steady", cycles_delta=0)


def test_summarize_battery_trend_is_unavailable_with_fewer_than_two_days_or_missing_fields() -> (
    None
):
    """Named Defect "missing read as zero": a single rollup day has no
    "change" to report at all -- a defect that defaulted to a
    fabricated 0.0 delta (rather than `None`) would claim a flat trend
    the device never actually reported. Same for a missing boundary
    `soh_last`/`cycles_last`."""
    one_day = [
        DailyBatteryTrend(
            day="2026-01-01",
            cycles_last=10,
            soh_last=98.0,
            soc_min=20,
            soc_max=95,
            batt_temp_max=25.0,
        )
    ]
    assert summarize_battery_trend(one_day) == BatteryTrendInsight(
        soh_delta=None, soh_direction=None, cycles_delta=None
    )

    missing_boundary = [
        DailyBatteryTrend(
            day="2026-01-01",
            cycles_last=None,
            soh_last=None,
            soc_min=20,
            soc_max=95,
            batt_temp_max=25.0,
        ),
        DailyBatteryTrend(
            day="2026-01-07",
            cycles_last=12,
            soh_last=97.0,
            soc_min=15,
            soc_max=90,
            batt_temp_max=26.0,
        ),
    ]
    assert summarize_battery_trend(missing_boundary) == BatteryTrendInsight(
        soh_delta=None, soh_direction=None, cycles_delta=None
    )
