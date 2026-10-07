"""Tests for `energy.accounting`: pure daily energy accounting from
cumulative Wh counters -- segmentation, the monotonicity guard, the
implausible-jump guard, gap-aware proration, and tariff-based cost.

Energy spec: "Daily Energy by Source and Direction", "Counter
Monotonicity Guard", "Energy Across a Data Gap Is Flagged", "Energy Cost
from a Configurable Flat Tariff", "Cost Basis Is Energy Drawn From the
Grid, Not Energy Delivered to Loads"; Named Defect "Counter reset";
design-data section 4.5.
"""

from __future__ import annotations

import pytest

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.energy.accounting import daily_energy


def _reading(**overrides: object) -> Reading:
    return Reading(**overrides)  # type: ignore[arg-type]


# --- Scenario: "A full day without a counter reset sums cleanly" / -------
# --- "No reset means a simple sum" ----------------------------------------


def test_a_full_day_without_a_reset_sums_to_the_exact_positive_deltas() -> None:
    """A defect that estimated or smoothed deltas instead of summing the
    exact counter differences would silently drift from what the device
    itself reported -- every source/direction field is exercised here,
    not just one, since a bug could plausibly affect only one column."""
    samples = [
        (0, _reading(chg_ac_wh=0.0, chg_dc_wh=0.0, chg_solar_wh=0.0, dsg_ac_wh=0.0, dsg_dc_wh=0.0)),
        (
            60,
            _reading(
                chg_ac_wh=50.0, chg_dc_wh=20.0, chg_solar_wh=10.0, dsg_ac_wh=5.0, dsg_dc_wh=2.0
            ),
        ),
        (
            120,
            _reading(
                chg_ac_wh=90.0, chg_dc_wh=35.0, chg_solar_wh=25.0, dsg_ac_wh=12.0, dsg_dc_wh=6.0
            ),
        ),
    ]

    result = daily_energy(samples, tz="UTC", tariff=None, currency="")

    day = result["1970-01-01"]
    assert (day.chg_ac_wh, day.chg_dc_wh, day.chg_solar_wh, day.dsg_ac_wh, day.dsg_dc_wh) == (
        90.0,
        35.0,
        25.0,
        12.0,
        6.0,
    )
    assert day.flags == frozenset()


# --- Scenario: "A counter reset does not produce negative energy" --------
# --- (Named Defect "Counter reset") ---------------------------------------


def test_a_counter_reset_contributes_zero_and_resumes_from_its_own_baseline() -> None:
    """Named Defect "Counter reset": a defect that let the decreased
    reading subtract from the running total would produce NEGATIVE daily
    energy -- the exact phantom-outage-adjacent mistake this whole
    project exists to avoid. The segment after the drop must resume from
    its own new baseline (80), not keep diffing against the pre-reset
    value (150)."""
    samples = [
        (0, _reading(chg_ac_wh=100.0)),
        (60, _reading(chg_ac_wh=150.0)),  # +50, valid
        (120, _reading(chg_ac_wh=80.0)),  # firmware reset: -70, must NOT subtract
        (180, _reading(chg_ac_wh=120.0)),  # +40 from the new baseline (80)
    ]

    result = daily_energy(samples, tz="UTC", tariff=None, currency="")

    day = result["1970-01-01"]
    assert day.chg_ac_wh == pytest.approx(90.0)  # 50 + 40, never negative
    assert day.chg_ac_wh >= 0.0
    assert "counter_reset" in day.flags


# --- Scenario (implausible jump, energy "Counter Monotonicity Guard" -----
# --- requirement's sibling guard) -----------------------------------------


def test_an_implausible_jump_is_flagged_and_contributes_zero() -> None:
    """A defect with no plausibility guard would accept a corrupted or
    momentarily-garbled counter reading at face value and inflate a
    day's energy far beyond what the device could physically have
    moved -- a delta averaging over 10 kW for its elapsed interval is
    discarded rather than trusted."""
    samples = [
        (0, _reading(chg_ac_wh=0.0)),
        (60, _reading(chg_ac_wh=5.0)),  # +5 Wh / 60s = 300 W avg: plausible
        (120, _reading(chg_ac_wh=305.0)),  # +300 Wh / 60s = 18,000 W avg: implausible
        (180, _reading(chg_ac_wh=310.0)),  # +5 Wh from 305 (the raw baseline): plausible
    ]

    result = daily_energy(samples, tz="UTC", tariff=None, currency="")

    day = result["1970-01-01"]
    assert day.chg_ac_wh == pytest.approx(10.0)  # 5 + 0 (discarded) + 5
    assert "implausible_jump" in day.flags


# --- Scenario: "A day with a gap is marked gap-affected" -----------------


def test_a_gap_spanning_a_day_boundary_prorates_the_share_to_each_day() -> None:
    """Energy requirement "Energy Across a Data Gap Is Flagged": a
    defect that attributed the whole delta to the day the gap ENDS on
    (the naive choice) would silently move energy into the wrong day --
    this gap starts 400 s before local midnight and ends 300 s after
    it, so a correct implementation must split the 70 Wh delta
    proportionally (400/700 to the first day, 300/700 to the second),
    flag both days gap-affected, and track each day's own prorated share
    of the AC charge-in counter specifically (`chg_ac_est_wh`)."""
    samples = [
        (86_000, _reading(chg_ac_wh=100.0)),  # 1970-01-01 23:53:20 UTC
        (86_700, _reading(chg_ac_wh=170.0)),  # 1970-01-02 00:05:00 UTC; gap = 700s
    ]

    result = daily_energy(samples, tz="UTC", tariff=None, currency="")

    first, second = result["1970-01-01"], result["1970-01-02"]
    assert first.chg_ac_wh == pytest.approx(70.0 * 400 / 700)
    assert second.chg_ac_wh == pytest.approx(70.0 * 300 / 700)
    assert "gap_prorated" in first.flags
    assert "gap_prorated" in second.flags
    assert first.chg_ac_est_wh == pytest.approx(first.chg_ac_wh)
    assert second.chg_ac_est_wh == pytest.approx(second.chg_ac_wh)


# --- Scenario: "Cost is price times AC charge-in energy" -----------------


def test_cost_equals_price_times_ac_charge_in_energy_with_the_configured_currency() -> None:
    """A defect that priced a different counter (DC or solar input,
    which the utility never bills for) would show an owner the wrong
    cost even though the kWh figures were each individually correct."""
    samples = [(0, _reading(chg_ac_wh=0.0)), (3_600, _reading(chg_ac_wh=2_000.0))]

    result = daily_energy(samples, tz="UTC", tariff=0.18, currency="HNL")

    day = result["1970-01-01"]
    assert day.cost == pytest.approx(2.0 * 0.18)  # 2 kWh * price
    assert day.currency == "HNL"


# --- Scenario: "No configured tariff shows energy without a fabricated --
# --- cost" ------------------------------------------------------------


def test_no_tariff_shows_energy_but_reports_cost_as_unavailable_not_zero() -> None:
    """The project's standing NULL-discipline applied to cost: a defect
    that defaulted an unconfigured tariff to `0.0` would make an owner
    who never set a price believe their energy was free, indistinguishable
    from an owner who configured a real $0 promotional rate."""
    samples = [(0, _reading(chg_ac_wh=0.0)), (3_600, _reading(chg_ac_wh=500.0))]

    result = daily_energy(samples, tz="UTC", tariff=None, currency="HNL")

    day = result["1970-01-01"]
    assert day.chg_ac_wh == pytest.approx(500.0)
    assert day.cost == "unavailable"
    assert day.cost != 0
    assert day.cost != 0.0


# --- Scenario: "Passthrough energy is included in the cost basis" --------
# --- (regression pinned to obs 5697's real measured evidence) ------------


def test_passthrough_ac_energy_is_included_in_the_cost_basis() -> None:
    """Spec requirement "Cost Basis Is Energy Drawn From the Grid, Not
    Energy Delivered to Loads": AC power the station passes straight
    through to a connected load, without ever charging the battery,
    must still count fully toward the cost basis -- "consistent with
    what the utility would bill". Pinned to real evidence rather than
    an arbitrary round number: `ecoflow-stats/verified-premises` (obs
    5697) measured the DELTA PRO's `dsg_ac_wh` counter against
    integrated `inv.outputWatts` on Daniel's real device and found them
    within 0.3%-1.8% of each other (1904 Wh counter delta vs 1898 Wh of
    integrated power, ratio 1.003; a 15-min check gave 1.018) -- i.e.
    the device's AC-side counters already track the FULL power moved,
    passthrough included, never a smaller "net of passthrough" figure.
    This fixture reuses that measured ~1904 Wh magnitude as `chg_ac_wh`'s
    delta during a passthrough day (SoC flat: the battery is neither
    charging nor discharging) and asserts a defect that excluded
    passthrough energy from the cost basis would under-bill what the
    utility actually delivered."""
    samples = [
        (0, _reading(chg_ac_wh=0.0, dsg_ac_wh=0.0, soc=50)),
        (28_440, _reading(chg_ac_wh=1_904.0, dsg_ac_wh=1_904.0, soc=50)),  # 7.9 h later
    ]

    result = daily_energy(samples, tz="UTC", tariff=0.15, currency="USD")

    day = result["1970-01-01"]
    assert day.chg_ac_wh == pytest.approx(1_904.0)
    assert day.cost == pytest.approx(1_904.0 / 1000 * 0.15)
