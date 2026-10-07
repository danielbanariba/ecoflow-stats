"""Unit tests for the pure `judge()` grid-presence verdict.

`judge` never sees raw payloads — it operates on already-normalized
`Reading` instances, so "the whole measurement row" is simply every
`Reading` field, and dataclass equality is the whole-row comparison.
"""

from __future__ import annotations

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.model import ABSENT, PRESENT, DetectorConfig, Judgment, judge

_CONFIG = DetectorConfig()


def test_grid_voltage_above_the_floor_judges_present() -> None:
    sample = Reading(grid_v=115.5)
    assert judge(sample, [], config=_CONFIG) == PRESENT


def test_grid_voltage_at_the_floor_judges_absent() -> None:
    """ "At or below" — exactly at the floor must not be treated as present."""
    sample = Reading(grid_v=_CONFIG.threshold_v)
    assert judge(sample, [], config=_CONFIG) == ABSENT


def test_grid_voltage_below_the_floor_judges_absent() -> None:
    sample = Reading(grid_v=0.0)
    assert judge(sample, [], config=_CONFIG) == ABSENT


def test_missing_grid_voltage_judges_unjudgeable_never_a_boolean_state() -> None:
    """Amendment A2's third moved scenario: grid-presence judgment
    requires grid_v. A missing value must never default to PRESENT or
    ABSENT — both would fabricate a reading the sample does not support."""
    sample = Reading(grid_v=None)
    result = judge(sample, [], config=_CONFIG)
    assert result == Judgment(state="unjudged", reason="unjudgeable")
    assert result not in (PRESENT, ABSENT)


def test_three_consecutive_identical_samples_the_third_is_stale_payload() -> None:
    """Amendment A3, scenario 1: a cached cloud payload repeats exactly;
    the rule is prospective, so only the third identical sample onward is
    flagged, never the first two."""
    sample = Reading(grid_v=120.0, ac_in_w=500.0, soc=80)
    previous_two = [sample, sample]
    result = judge(sample, previous_two, config=_CONFIG)
    assert result == Judgment(state="unjudged", reason="stale_payload")


def test_exactly_two_consecutive_identical_samples_judge_normally() -> None:
    """Amendment A3, scenario 2: with only one real predecessor on record,
    this is not yet the third repeat — it must judge on its own merits."""
    sample = Reading(grid_v=120.0, ac_in_w=500.0, soc=80)
    previous_one = [sample]
    assert judge(sample, previous_one, config=_CONFIG) == PRESENT


def test_only_one_field_repeating_across_pairs_still_judges_normally() -> None:
    """Amendment A3, scenario 3 (regression, verified-premises obs 5697:
    22 consecutive `inv.outputWatts`-only repeats with no false stale call
    on real data): staleness must compare the whole row, never one field.
    """
    current = Reading(grid_v=120.0, ac_out_w=300.0, soc=81)
    previous_two = [
        Reading(grid_v=119.8, ac_out_w=300.0, soc=80),
        Reading(grid_v=120.1, ac_out_w=300.0, soc=79),
    ]
    assert judge(current, previous_two, config=_CONFIG) == PRESENT
