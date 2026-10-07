"""Tests for the pure reconciliation layer `outages.resolve.resolve`.

Outages requirements "A Data Gap Is Never Classified as an Outage"
(aggregation half: "An unresolved gap stays out of outage totals"), "A
User Decision on a Gap Is Durable and Survives Recomputation", "Outage
Aggregates" ("An unconfirmed suspected phantom is excluded until
confirmed"); history-import "Recomputed Events Are Authoritative Where
the App's Own Data Overlaps" (both scenarios: overlapping period counted
from recomputation with the import kept as provenance; non-overlapping
period counted directly from the import). Named Defect "Phantom"
(reconciliation half).
"""

from __future__ import annotations

from ecoflow_stats.outages.model import Decision, Event, Gap
from ecoflow_stats.outages.resolve import resolve
from ecoflow_stats.storage.legacy import LegacyOutage

_RANGE_END = 100_000


def _gap(start_ts: int, end_ts: int | None, **overrides: object) -> Gap:
    defaults: dict[str, object] = {
        "cause": "unknown",
        "state_before": "present",
        "state_after": "present",
        "soc_before": 80,
        "soc_after": 75,
        "chg_ac_wh_delta": None,
        "expected_in_wh": None,
        "evidence": "inconclusive",
        "failures": {},
    }
    defaults.update(overrides)
    return Gap(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _decision(
    target: str, start_ts: int, end_ts: int, verdict: str, **overrides: object
) -> Decision:
    defaults: dict[str, object] = {"device_id": 1, "decided_at": 1}
    defaults.update(overrides)
    return Decision(target=target, start_ts=start_ts, end_ts=end_ts, verdict=verdict, **defaults)  # type: ignore[arg-type]


def _legacy(id_: int, start_ts: int, end_ts: int | None, **overrides: object) -> LegacyOutage:
    defaults: dict[str, object] = {
        "device_id": 1,
        "soc_start": 50,
        "soc_end": 60,
        "logged_minutes": None,
        "start_line": None,
        "end_line": None,
        "source_tz": "UTC",
        "flags": frozenset(),
        "import_id": 1,
    }
    defaults.update(overrides)
    return LegacyOutage(id=id_, start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def test_an_unresolved_gap_with_no_decision_stays_excluded_from_totals() -> None:
    """Pass-1: without this, every unreviewed gap would silently count
    as downtime the moment it is detected, defeating the whole point of
    a review queue (outages: "A Data Gap Is Never Classified as an
    Outage")."""
    view = resolve(
        detected=[], gaps=[_gap(100, 400)], legacy=[], decisions=[], range_end=_RANGE_END
    )

    assert view.outages == []


def test_a_confirmed_gap_becomes_a_counted_outage_with_both_boundaries_uncertain() -> None:
    decision = _decision("gap", 100, 400, "outage")
    view = resolve(
        detected=[], gaps=[_gap(100, 400)], legacy=[], decisions=[decision], range_end=_RANGE_END
    )

    assert len(view.outages) == 1
    outage = view.outages[0]
    assert outage.source == "gap"
    assert (outage.start_ts, outage.end_ts) == (100, 400)
    assert outage.start_uncertain is True
    assert outage.end_uncertain is True


def test_a_rejected_gap_stays_excluded() -> None:
    decision = _decision("gap", 100, 400, "no_outage")
    view = resolve(
        detected=[], gaps=[_gap(100, 400)], legacy=[], decisions=[decision], range_end=_RANGE_END
    )

    assert view.outages == []


def test_a_decision_overlapping_a_gap_by_exactly_half_still_matches() -> None:
    """Pass-1: an off-by-one in the >=50%-overlap boundary would make a
    decision recorded against a slightly different (but clearly the
    same) gap interval silently fail to apply. Both intervals are 200s
    long and overlap over [200,300] -- 100s, exactly 50% of either."""
    decision = _decision("gap", 100, 300, "outage")
    view = resolve(
        detected=[], gaps=[_gap(200, 400)], legacy=[], decisions=[decision], range_end=_RANGE_END
    )

    assert len(view.outages) == 1


def test_a_decision_overlapping_a_gap_by_less_than_half_does_not_match() -> None:
    """The decision is 160s long ([100,260]); it overlaps the 200s gap
    ([200,400]) over only [200,260] -- 60s, 37.5% of the shorter (160s)
    interval."""
    decision = _decision("gap", 100, 260, "outage")
    view = resolve(
        detected=[], gaps=[_gap(200, 400)], legacy=[], decisions=[decision], range_end=_RANGE_END
    )

    assert view.outages == []


def test_a_legacy_event_matching_a_detected_event_links_and_is_not_double_counted() -> None:
    """history-import: "Overlapping period is counted from
    recomputation, with the import kept as provenance." Pass-1: without
    the match, the same real outage would be counted twice -- once from
    the detector, once from the legacy import."""
    event = Event(start_ts=1_000, kind="outage", end_ts=1_300, readings=6)
    legacy_entry = _legacy(7, 1_010, 1_290)

    view = resolve(
        detected=[event], gaps=[], legacy=[legacy_entry], decisions=[], range_end=_RANGE_END
    )

    assert len(view.outages) == 1  # the detected event, not a second copy
    assert view.outages[0].source == "detected"
    assert event.legacy_id == 7
    assert len(view.legacy_statuses) == 1
    assert view.legacy_statuses[0].legacy_id == 7
    assert view.legacy_statuses[0].status == "matched"


def test_a_non_overlapping_legacy_event_is_counted_directly_from_the_import() -> None:
    """history-import: "Non-overlapping period is counted directly from
    the import."""
    legacy_entry = _legacy(9, 5_000, 5_300)

    view = resolve(detected=[], gaps=[], legacy=[legacy_entry], decisions=[], range_end=_RANGE_END)

    assert len(view.outages) == 1
    assert view.outages[0].source == "legacy"
    assert (view.outages[0].start_ts, view.outages[0].end_ts) == (5_000, 5_300)


def test_an_unconfirmed_suspected_phantom_legacy_event_is_excluded_until_confirmed() -> None:
    """Named Defect "Phantom": a logged zero-charge outage-start must
    never silently count as real downtime before a human confirms it."""
    legacy_entry = _legacy(3, 2_000, 2_100, flags=frozenset({"suspected_phantom"}))

    view = resolve(detected=[], gaps=[], legacy=[legacy_entry], decisions=[], range_end=_RANGE_END)

    assert view.outages == []
    assert view.legacy_statuses[0].status == "excluded_phantom"


def test_confirming_a_suspected_phantom_as_real_includes_it() -> None:
    legacy_entry = _legacy(3, 2_000, 2_100, flags=frozenset({"suspected_phantom"}))
    decision = _decision("legacy", 2_000, 2_100, "real")

    view = resolve(
        detected=[], gaps=[], legacy=[legacy_entry], decisions=[decision], range_end=_RANGE_END
    )

    assert len(view.outages) == 1
    assert view.legacy_statuses[0].status == "counted"


def test_detected_outage_events_are_always_counted_without_a_decision() -> None:
    event = Event(start_ts=10, kind="outage", end_ts=200, readings=3)

    view = resolve(detected=[event], gaps=[], legacy=[], decisions=[], range_end=_RANGE_END)

    assert len(view.outages) == 1
    assert view.outages[0].source == "detected"


def test_brief_events_are_listed_separately_and_never_counted() -> None:
    brief = Event(start_ts=10, kind="brief", end_ts=70, readings=1)

    view = resolve(detected=[brief], gaps=[], legacy=[], decisions=[], range_end=_RANGE_END)

    assert view.outages == []
    assert len(view.briefs) == 1


def test_a_decision_matching_nothing_is_reported_as_orphaned() -> None:
    """Pass-1: a decision whose target vanished (the gap got re-resolved
    into a different boundary by a later recompute, say) must still be
    visible SOMEWHERE -- never just silently dropped from view."""
    decision = _decision("gap", 50_000, 50_100, "outage")

    view = resolve(detected=[], gaps=[], legacy=[], decisions=[decision], range_end=_RANGE_END)

    assert view.orphaned_decisions == [decision]
