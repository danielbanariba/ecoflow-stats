"""Unit tests for `evidence.make_gap`: a gap's cause and its
likely-present/absent classification, built purely from the two judged
readings bracketing it plus whatever failure/app-run/unjudged context is
given — never an outage by inference (design D5).
"""

from __future__ import annotations

from ecoflow_stats.outages.evidence import make_gap
from ecoflow_stats.outages.model import JudgedPoint
from ecoflow_stats.storage.failures import FetchFailure
from ecoflow_stats.storage.runs import AppRun

_COVERING_RUN = AppRun(
    id=1, started_at=0, last_tick_at=10_000, stopped_at=None, app_version="0.1.0"
)


def _point(
    ts: int,
    *,
    state: str = "present",
    soc: int | None = 80,
    chg_ac_wh: float | None = 0.0,
    ac_in_w: float | None = 500.0,
) -> JudgedPoint:
    return JudgedPoint(ts=ts, state=state, soc=soc, chg_ac_wh=chg_ac_wh, ac_in_w=ac_in_w)


def test_a_single_failure_category_inside_the_window_becomes_the_cause() -> None:
    """One category of fetch failure fully explains the gap: the cause is
    that category, not "mixed" or "unknown"."""
    before, after = _point(0), _point(240)
    failures = [FetchFailure(ts=120, outcome="network", code=None, attempts=2, latency_ms=500)]

    gap = make_gap(before, after, failures=failures, app_runs=[_COVERING_RUN])

    assert gap.cause == "network"
    assert gap.failures == {"network": 1}


def test_a_failure_outside_the_window_is_excluded_from_the_cause() -> None:
    """ "Inside the gap window" is a real boundary, not a hint: a failure
    recorded before the gap even started must not be blamed for it."""
    before, after = _point(100), _point(200)
    failures = [FetchFailure(ts=50, outcome="network", code=None, attempts=1, latency_ms=None)]

    gap = make_gap(before, after, failures=failures, app_runs=[_COVERING_RUN])

    assert gap.cause == "unknown"
    assert gap.failures == {}


def test_a_failure_category_plus_an_unjudged_reason_gives_mixed() -> None:
    """Two different explanations for the same silent window must not be
    collapsed into one: "mixed" tells a reviewer there is more than one
    story, which a single clean category would hide."""
    before, after = _point(0), _point(240)
    failures = [FetchFailure(ts=120, outcome="timeout", code=None, attempts=3, latency_ms=12000)]

    gap = make_gap(
        before, after, failures=failures, pending={"stale_payload": 1}, app_runs=[_COVERING_RUN]
    )

    assert gap.cause == "mixed"
    assert gap.failures == {"timeout": 1, "stale_payload": 1}


def test_a_failure_code_is_kept_in_the_detail_but_not_in_the_coarse_cause() -> None:
    """The detailed breakdown keeps the code for forensic value, but the
    coarse `cause` groups by outcome only — two different codes for the
    same outcome are still one category, not "mixed"."""
    before, after = _point(0), _point(240)
    failures = [
        FetchFailure(ts=100, outcome="api", code="8521", attempts=1, latency_ms=200),
        FetchFailure(ts=200, outcome="api", code="8522", attempts=1, latency_ms=200),
    ]

    gap = make_gap(before, after, failures=failures, app_runs=[_COVERING_RUN])

    assert gap.cause == "api"
    assert gap.failures == {"api:8521": 1, "api:8522": 1}


def test_no_covering_app_run_adds_app_down_to_the_cause() -> None:
    """When no recorded app run covers any part of the gap, the app
    itself was not running — that must read as "app_down", not be
    blamed on the cloud API."""
    before, after = _point(0), _point(240)

    gap = make_gap(before, after, app_runs=[])

    assert gap.cause == "app_down"
    assert gap.failures == {"app_down": 1}


def test_no_recorded_explanation_at_all_gives_unknown_not_a_guess() -> None:
    """With the app confirmed running (so not "app_down") and no failure
    or unjudged reason recorded for the window, there is genuinely no
    explanation on record — the honest answer is "unknown", never a
    fabricated guess."""
    before, after = _point(0), _point(240)

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.cause == "unknown"
    assert gap.failures == {}


def test_a_high_energy_delta_against_expectation_is_likely_present() -> None:
    """Ratio thresholds (design-data section 4.2): a delta at or above
    80% of the expected input energy means the grid was likely present
    throughout, even with no direct reading inside the gap."""
    before = _point(0, ac_in_w=600.0, chg_ac_wh=1000.0)
    after = _point(240, ac_in_w=600.0, chg_ac_wh=1039.0)  # expected 40 Wh; delta 39 -> ratio 0.975

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.evidence == "likely_present"


def test_a_low_energy_delta_against_expectation_is_likely_absent() -> None:
    before = _point(0, ac_in_w=600.0, chg_ac_wh=1000.0)
    after = _point(240, ac_in_w=600.0, chg_ac_wh=1002.0)  # expected 40 Wh; delta 2 -> ratio 0.05

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.evidence == "likely_absent"


def test_a_mid_range_energy_delta_is_inconclusive() -> None:
    before = _point(0, ac_in_w=600.0, chg_ac_wh=1000.0)
    after = _point(240, ac_in_w=600.0, chg_ac_wh=1020.0)  # expected 40 Wh; delta 20 -> ratio 0.5

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.evidence == "inconclusive"


def test_a_negative_counter_delta_is_inconclusive_not_likely_absent() -> None:
    """A counter reset during the gap must not be read as "definitely no
    charging happened" — it is simply unreadable evidence (Named Defect
    "Counter reset", evidence half)."""
    before = _point(0, ac_in_w=600.0, chg_ac_wh=1000.0)
    after = _point(240, ac_in_w=600.0, chg_ac_wh=0.0)

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.evidence == "inconclusive"


def test_missing_boundary_energy_data_is_inconclusive() -> None:
    """Without both boundary readings' AC-in watts, there is no expected-
    energy estimate to compare against — the honest answer is
    "inconclusive", never a guessed verdict."""
    before = _point(0, ac_in_w=None, chg_ac_wh=1000.0)
    after = _point(240, ac_in_w=600.0, chg_ac_wh=1010.0)

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.evidence == "inconclusive"


def test_a_gap_carries_its_before_and_after_evidence() -> None:
    """Scenario "A gap is recorded with its before/after evidence": the
    readings and timestamps immediately preceding and following it must
    be visible on the gap itself, not discarded."""
    before = _point(100, state="present", soc=90)
    after = _point(400, state="present", soc=88)

    gap = make_gap(before, after, app_runs=[_COVERING_RUN])

    assert gap.start_ts == 100
    assert gap.end_ts == 400
    assert gap.state_before == "present"
    assert gap.state_after == "present"
    assert gap.soc_before == 90
    assert gap.soc_after == 88
