"""Reconciliation: turn detected events, recorded gaps, imported legacy
outage events and durable user decisions into one effective view of
what actually counts as downtime (design-data section 4.4).

Satisfies outages "A Data Gap Is Never Classified as an Outage"
(aggregation half: an unresolved gap stays excluded), "A User Decision
on a Gap Is Durable and Survives Recomputation"; history-import
"Recomputed Events Are Authoritative Where the App's Own Data Overlaps"
(both scenarios); Named Defect "Phantom" (reconciliation half).

Pure: depends only on `outages.model`'s value types and the structural
`LegacyOutageLike` Protocol, never on `storage` -- the same pure-core
boundary `detector.py`/`service.py` already keep
(`tests/contract/test_pure_core_imports.py`).

Deliberately simpler than design-data 4.4's own pseudocode in two ways,
both because `resolve()` never receives raw samples (only already-
derived events/gaps), and because no task scenario in this PR exercises
the dropped behavior:

- No "contradicted" legacy status (a legacy entry whose window is
  covered by real samples that show no outage at all) -- telling
  "covered by quiet presence" apart from "never covered at all" needs
  raw sample access this pure function does not have. A legacy entry
  either matches a detected event (linked, not double-counted) or it
  does not (counted directly unless phantom-and-unconfirmed); this
  resolves both required history-import scenarios without it.
- No `merge_overlapping_or_adjacent` pass (an outage that runs directly
  into a confirmed gap stays two adjacent `EffectiveOutage` entries
  rather than merging into one). Left for a later page-layer
  refinement if it turns out to matter for a real recorded sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.outages.model import Decision, Event, Gap, LegacyOutageLike

_MIN_OVERLAP_RATIO = 0.5
"""A decision matches a gap/legacy target when their intervals overlap
by at least this fraction of the SHORTER interval (design-data 4.4)."""
_LEGACY_MATCH_TOLERANCE_S = 90
"""How far a legacy entry's boundary may drift from a detected event's
own boundary and still count as the same real outage (design-data 4.4:
`overlaps_detected(L, tolerance=90s)`)."""


@dataclass(frozen=True, slots=True)
class EffectiveOutage:
    """One outage counted in statistics, from whichever source decided
    it: a detected (debounced) event, a user-confirmed gap, or a
    non-overlapping imported legacy event."""

    start_ts: int
    end_ts: int | None
    kind: Literal["outage", "brief"]
    source: Literal["detected", "gap", "legacy"]
    start_uncertain: bool
    end_uncertain: bool
    soc_start: int | None
    soc_end: int | None
    legacy_id: int | None = None


@dataclass(frozen=True, slots=True)
class LegacyStatus:
    """What became of one imported legacy outage event."""

    legacy_id: int
    status: Literal["matched", "excluded_phantom", "counted"]


@dataclass(frozen=True, slots=True)
class EffectiveView:
    """The full reconciled picture `resolve()` produces for one device."""

    outages: list[EffectiveOutage]
    briefs: list[EffectiveOutage]
    legacy_statuses: list[LegacyStatus]
    orphaned_decisions: list[Decision]


def _closed_end(end_ts: int | None, open_end: int) -> int:
    """`end_ts`, or `open_end` when still-ongoing (`end_ts is None`)."""
    return end_ts if end_ts is not None else open_end


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def _overlap_ratio(d_start: int, d_end: int, t_start: int, t_end: int) -> float:
    """Overlap length divided by the shorter of the two closed
    intervals. `Decision.end_ts` is always set (the schema's own `NOT
    NULL`), so only the target side (a gap, a legacy event) ever needs
    an open-end substitution before calling this."""
    overlap = min(d_end, t_end) - max(d_start, t_start)
    if overlap <= 0:
        return 0.0
    shorter = min(d_end - d_start, t_end - t_start)
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _best_decision(
    decisions: Sequence[Decision],
    target: Literal["gap", "legacy"],
    start_ts: int,
    end_ts: int,
) -> Decision | None:
    """The most recently decided active decision whose interval overlaps
    this target's by at least `_MIN_OVERLAP_RATIO`, or `None`."""
    candidates = [
        d
        for d in decisions
        if d.target == target
        and d.superseded_by is None
        and _overlap_ratio(d.start_ts, d.end_ts, start_ts, end_ts) >= _MIN_OVERLAP_RATIO
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.decided_at)


def _from_event(event: Event) -> EffectiveOutage:
    return EffectiveOutage(
        start_ts=event.start_ts,
        end_ts=event.end_ts,
        kind=event.kind,
        source="detected",
        start_uncertain=event.start_in_gap,
        end_uncertain=event.end_in_gap,
        soc_start=event.soc_start,
        soc_end=event.soc_end,
        legacy_id=event.legacy_id,
    )


def _from_gap(gap: Gap) -> EffectiveOutage:
    return EffectiveOutage(
        start_ts=gap.start_ts,
        end_ts=gap.end_ts,
        kind="outage",
        source="gap",
        start_uncertain=True,
        end_uncertain=True,
        soc_start=gap.soc_before,
        soc_end=gap.soc_after,
    )


def _from_legacy(entry: LegacyOutageLike) -> EffectiveOutage:
    return EffectiveOutage(
        start_ts=entry.start_ts,
        end_ts=entry.end_ts,
        kind="outage",
        source="legacy",
        start_uncertain=True,
        end_uncertain=True,
        soc_start=entry.soc_start,
        soc_end=entry.soc_end,
        legacy_id=entry.id,
    )


def _find_matching_event(
    entry: LegacyOutageLike, detected: Sequence[Event], range_end: int
) -> Event | None:
    window_start = entry.start_ts - _LEGACY_MATCH_TOLERANCE_S
    window_end = _closed_end(entry.end_ts, entry.start_ts) + _LEGACY_MATCH_TOLERANCE_S
    return next(
        (
            event
            for event in detected
            if event.kind == "outage"
            and _intervals_overlap(
                event.start_ts, _closed_end(event.end_ts, range_end), window_start, window_end
            )
        ),
        None,
    )


def resolve(
    *,
    detected: Sequence[Event],
    gaps: Sequence[Gap],
    legacy: Sequence[LegacyOutageLike],
    decisions: Sequence[Decision],
    range_end: int,
) -> EffectiveView:
    """Reconcile every source into one effective view.

    `range_end` substitutes only for an open-ended gap's or event's
    missing `end_ts` when computing a decision's overlap ratio or a
    legacy-event match -- an ongoing period is never treated as having
    already ended. Range-clipped totals for display are a separate
    concern of `aggregates.compute_aggregates`.
    """
    outages = [_from_event(event) for event in detected if event.kind == "outage"]
    briefs = [_from_event(event) for event in detected if event.kind == "brief"]
    used_decision_ids: set[int] = set()

    for gap in gaps:
        gap_end = _closed_end(gap.end_ts, range_end)
        decision = _best_decision(decisions, "gap", gap.start_ts, gap_end)
        if decision is None:
            continue
        used_decision_ids.add(id(decision))
        if decision.verdict == "outage":
            outages.append(_from_gap(gap))

    legacy_statuses: list[LegacyStatus] = []
    for entry in legacy:
        matching_event = _find_matching_event(entry, detected, range_end)
        if matching_event is not None:
            matching_event.legacy_id = entry.id
            legacy_statuses.append(LegacyStatus(legacy_id=entry.id, status="matched"))
            continue

        entry_end = _closed_end(entry.end_ts, entry.start_ts)
        decision = _best_decision(decisions, "legacy", entry.start_ts, entry_end)
        if decision is not None:
            used_decision_ids.add(id(decision))
        is_phantom = (decision is not None and decision.verdict == "phantom") or (
            "suspected_phantom" in entry.flags
            and not (decision is not None and decision.verdict == "real")
        )
        if is_phantom:
            legacy_statuses.append(LegacyStatus(legacy_id=entry.id, status="excluded_phantom"))
        else:
            legacy_statuses.append(LegacyStatus(legacy_id=entry.id, status="counted"))
            outages.append(_from_legacy(entry))

    orphaned = [
        decision
        for decision in decisions
        if decision.superseded_by is None and id(decision) not in used_decision_ids
    ]
    return EffectiveView(
        outages=outages,
        briefs=briefs,
        legacy_statuses=legacy_statuses,
        orphaned_decisions=orphaned,
    )


__all__ = ["EffectiveOutage", "EffectiveView", "LegacyStatus", "resolve"]
