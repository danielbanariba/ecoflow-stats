"""Tests for `battery.service.observed_autonomy`: comparing one
outage's observed battery autonomy against the device's own
remaining-time estimate at the moment it began.

Battery requirement "Observed Autonomy Compared With Device Estimate";
amendment item 7 (the comparison is "unavailable", never fabricated,
when the device reported no estimate); design-data section 4.6.
"""

from __future__ import annotations

from ecoflow_stats.battery.service import ObservedAutonomy, observed_autonomy
from ecoflow_stats.outages.model import Event


def _event(**overrides: object) -> Event:
    defaults: dict[str, object] = {
        "start_ts": 0,
        "end_ts": 7_200,
        "kind": "outage",
        "soc_start": 90,
        "soc_min": 60,
        "dsg_remain_min_start": 240,
    }
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


def test_observed_duration_is_shown_alongside_the_devices_estimate() -> None:
    """Scenario "Observed duration is shown alongside the device's
    estimate": a qualifying outage (>= 15 min, >= 3-point drop) with a
    known device estimate must report both numbers. Pass-1: a defect
    that dropped the device-estimate comparison, or mis-derived the
    observed rate, would leave the battery page's autonomy table unable
    to show the one comparison it exists for."""
    event = _event()

    result = observed_autonomy(event)

    assert result == ObservedAutonomy(event_start_ts=0, observed_h=6.0, device_estimate_h=4.0)


def test_a_missing_device_estimate_is_reported_as_unavailable_not_fabricated() -> None:
    """Amendment item 7, scenario "A missing estimate is reported as
    unavailable, not fabricated": a defect that defaulted a missing
    `dsg_remain_min_start` to `0` (or omitted the field) would either
    fabricate a comparison the device never reported, or silently drop
    it instead of saying so."""
    event = _event(dsg_remain_min_start=None)

    result = observed_autonomy(event)

    assert result == ObservedAutonomy(
        event_start_ts=0, observed_h=6.0, device_estimate_h="unavailable"
    )


def test_observed_duration_and_estimate_at_the_exact_qualifying_boundary() -> None:
    """Boundary case for "at least 15 min" and "at least 3 points": a
    defect using a strict `>` instead of `>=` for either threshold
    would wrongly discard an outage that exactly meets the design's own
    documented floor."""
    event = _event(end_ts=900, soc_start=90, soc_min=87, dsg_remain_min_start=30)

    result = observed_autonomy(event)

    assert result != "not enough data"
    assert isinstance(result, ObservedAutonomy)


def test_an_outage_shorter_than_15_minutes_is_not_enough_data_even_with_a_big_drop() -> None:
    """The too-short qualification guard: a defect that only checked the
    SoC drop (ignoring duration) would extrapolate an hourly rate from a
    handful of seconds, producing a wildly unreliable autonomy figure
    instead of honestly admitting there is not enough data."""
    event = _event(end_ts=600, soc_start=90, soc_min=60)

    assert observed_autonomy(event) == "not enough data"


def test_an_outage_with_too_shallow_a_drop_is_not_enough_data_even_with_long_duration() -> None:
    """The too-shallow qualification guard: a defect that only checked
    duration (ignoring the drop) would extrapolate a rate from noise-
    level SoC movement over a long outage, instead of admitting there
    is not enough data."""
    event = _event(soc_start=90, soc_min=88)

    assert observed_autonomy(event) == "not enough data"


def test_a_missing_starting_charge_is_not_enough_data_not_a_crash() -> None:
    """Reuses `battery.stats.depth_of_discharge`'s own NULL-safety
    branch rather than re-deriving the `soc_start is None` guard here a
    second time (task 17.11 REFACTOR). Pass-1: a defect that duplicated
    the guard and got one operand wrong (or omitted it) would crash with
    a `TypeError` on a real device that stopped reporting charge mid-
    outage, instead of degrading to an honest "not enough data"."""
    event = _event(soc_start=None)

    assert observed_autonomy(event) == "not enough data"


def test_a_missing_lowest_point_is_not_enough_data_not_a_crash() -> None:
    """The symmetric half of the previous test -- proven independently
    rather than assuming one operand's guard already covers both,
    exactly as `test_stats.py` proves both `depth_of_discharge` operands
    separately."""
    event = _event(soc_min=None)

    assert observed_autonomy(event) == "not enough data"


def test_an_ongoing_outage_is_not_enough_data_not_a_guess() -> None:
    """An outage still in progress (`end_ts is None`) has no final
    duration or final `soc_min` to extrapolate from yet. Pass-1: a
    defect that substituted "now" for the missing `end_ts` would report
    a comparison that keeps changing on every request for an outage
    that has not actually finished, instead of waiting for it to end."""
    event = _event(end_ts=None)

    assert observed_autonomy(event) == "not enough data"
