"""Gap cause and evidence: turns the silent window between two judged
readings into an honest, reviewable record — never an outage by
inference (design D5).

`make_gap` is the one place that decides a gap's `cause` and `evidence`;
it is given the raw candidate failures/app-runs/unjudged-reasons (not
pre-windowed by its caller) so the "inside the gap window" and "covers
part of the gap" rules are directly testable here, in isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.outages.model import Gap

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ecoflow_stats.outages.model import AppRunLike, FailureLike, JudgedPoint

_EXPECTED_ENERGY_FLOOR_WH = 5.0
"""Below this expected input energy, a ratio is too noisy to classify
(design-data section 4.2) — the gap stays `inconclusive` rather than
divide by a near-zero expectation."""
_LIKELY_PRESENT_RATIO = 0.8
_LIKELY_ABSENT_RATIO = 0.2


def _covering_run_end(run: AppRunLike) -> int:
    """`COALESCE(stopped_at, last_tick_at, started_at)` — mirrors
    `storage.runs.RunLog.covering`'s own SQL exactly, so a gap's "was the
    app running" check agrees with the store's own query."""
    if run.stopped_at is not None:
        return run.stopped_at
    if run.last_tick_at is not None:
        return run.last_tick_at
    return run.started_at


def make_gap(
    before: JudgedPoint,
    after: JudgedPoint,
    *,
    failures: Sequence[FailureLike] = (),
    pending: Mapping[str, int] | None = None,
    app_runs: Sequence[AppRunLike] = (),
) -> Gap:
    """Build one `Gap` from the two judged readings bracketing it.

    One category (a failure outcome, an unjudged reason, or `app_down`)
    gives that cause; several give `mixed`; none at all gives `unknown`
    rather than guessing (design-data section 4.2).
    """
    pending = pending or {}
    relevant_failures = [f for f in failures if before.ts < f.ts < after.ts]

    detail: dict[str, int] = {}
    for failure in relevant_failures:
        key = f"{failure.outcome}:{failure.code}" if failure.code else failure.outcome
        detail[key] = detail.get(key, 0) + 1
    for reason, count in pending.items():
        detail[reason] = detail.get(reason, 0) + count

    covered = any(
        run.started_at <= after.ts and _covering_run_end(run) >= before.ts for run in app_runs
    )
    cause_categories = {failure.outcome for failure in relevant_failures} | set(pending.keys())
    if not covered:
        detail["app_down"] = detail.get("app_down", 0) + 1
        cause_categories.add("app_down")

    if not cause_categories:
        cause = "unknown"
    elif len(cause_categories) == 1:
        cause = next(iter(cause_categories))
    else:
        cause = "mixed"

    expected_in_wh: float | None = None
    if before.ac_in_w is not None and after.ac_in_w is not None:
        expected_in_wh = (before.ac_in_w + after.ac_in_w) / 2 * (after.ts - before.ts) / 3600

    chg_ac_wh_delta: float | None = None
    if before.chg_ac_wh is not None and after.chg_ac_wh is not None:
        chg_ac_wh_delta = after.chg_ac_wh - before.chg_ac_wh

    if (
        chg_ac_wh_delta is None
        or chg_ac_wh_delta < 0
        or expected_in_wh is None
        or expected_in_wh < _EXPECTED_ENERGY_FLOOR_WH
    ):
        gap_evidence = "inconclusive"
    else:
        ratio = chg_ac_wh_delta / expected_in_wh
        if ratio >= _LIKELY_PRESENT_RATIO:
            gap_evidence = "likely_present"
        elif ratio <= _LIKELY_ABSENT_RATIO:
            gap_evidence = "likely_absent"
        else:
            gap_evidence = "inconclusive"

    return Gap(
        start_ts=before.ts,
        end_ts=after.ts,
        cause=cause,
        state_before=before.state,
        state_after=after.state,
        soc_before=before.soc,
        soc_after=after.soc,
        chg_ac_wh_delta=chg_ac_wh_delta,
        expected_in_wh=expected_in_wh,
        evidence=gap_evidence,
        failures=detail,
    )


__all__ = ["make_gap"]
