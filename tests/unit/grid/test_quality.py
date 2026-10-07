"""Unit tests for the pure grid-quality core (grid-quality spec: "Grid
Voltage and Frequency History", "Daily Voltage and Frequency Ranges",
"Grid-Quality Metrics Exclude Periods Without Grid Voltage").

Reuses `outages.model.judge()` for grid presence -- the exact same
threshold the detector and live status already use -- rather than a
second hand-rolled `grid_v > threshold` comparison; no fake/mock is
needed since `judge()` is itself pure.
"""

from __future__ import annotations

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.grid.quality import GridRange, grid_quality_range, voltage_frequency_history


def _reading(**overrides: object) -> Reading:
    return Reading(**overrides)  # type: ignore[arg-type]


def test_voltage_frequency_history_reflects_recorded_samples_in_order() -> None:
    """Scenario "History reflects recorded samples in order". Pass-1:
    catches a defect that returns samples in query/insertion order
    instead of defensively sorting by `ts`, the same convention
    `battery.stats.charge_history` already established."""
    unordered = [
        (200, _reading(grid_v=121.0, grid_hz=60.0)),
        (100, _reading(grid_v=120.0, grid_hz=59.9)),
        (300, _reading(grid_v=122.0, grid_hz=60.1)),
    ]

    history = voltage_frequency_history(unordered)

    assert [point.ts for point in history] == [100, 200, 300]
    assert [point.grid_v for point in history] == [120.0, 121.0, 122.0]
    assert [point.grid_hz for point in history] == [59.9, 60.0, 60.1]


def test_a_batch_of_grid_present_samples_reports_min_avg_max_voltage_and_frequency() -> None:
    """Scenario "A normal day reports a voltage and frequency range".
    Pass-1: catches wrong min/avg/max arithmetic, or a defect that
    reports the wrong `readings` count."""
    samples = [
        (0, _reading(grid_v=118.0, grid_hz=59.8)),
        (60, _reading(grid_v=120.0, grid_hz=60.0)),
        (120, _reading(grid_v=122.0, grid_hz=60.2)),
    ]

    result = grid_quality_range(samples)

    assert isinstance(result, GridRange)
    assert (result.grid_v_min, result.grid_v_avg, result.grid_v_max) == (118.0, 120.0, 122.0)
    assert result.grid_hz_min == 59.8
    assert result.grid_hz_max == 60.2
    assert result.readings == 3


def test_a_batch_with_no_grid_present_samples_reports_unavailable() -> None:
    """Scenario "A day with no grid-present samples reports no range".
    Pass-1: catches a defect that fabricates a `0.0` range (or an
    empty-but-truthy object) instead of the honest `"unavailable"`
    sentinel when every reading judges absent."""
    all_absent = [
        (0, _reading(grid_v=10.0)),  # below the 50V floor: absent
        (60, _reading(grid_v=5.0)),
    ]

    result = grid_quality_range(all_absent)

    assert result == "unavailable"


def test_an_empty_batch_reports_unavailable() -> None:
    """An outage spanning the whole day leaves zero samples at all for
    that day; this must behave identically to "every sample judged
    absent", not raise on an empty `min()`/`max()` call."""
    result = grid_quality_range([])

    assert result == "unavailable"


def test_a_null_voltage_sample_is_excluded_and_does_not_pull_the_minimum_toward_zero() -> None:
    """Scenario "A NULL voltage sample does not pull the range toward
    zero" (Grid-Quality Metrics Exclude Periods Without Grid Voltage).
    Pass-1: catches a defect that coerces a missing `grid_v` to `0.0`
    before computing `min()`, which would silently drag every day's
    minimum toward zero the moment one reading lacks voltage at all."""
    samples = [
        (0, _reading(grid_v=118.0, grid_hz=59.9)),
        (60, _reading(grid_v=None, grid_hz=None)),  # unjudgeable: excluded entirely
        (120, _reading(grid_v=122.0, grid_hz=60.1)),
    ]

    result = grid_quality_range(samples)

    assert isinstance(result, GridRange)
    assert result.grid_v_min == 118.0
    assert result.readings == 2
