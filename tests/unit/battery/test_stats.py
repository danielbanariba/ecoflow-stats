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
