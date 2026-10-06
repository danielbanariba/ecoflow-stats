"""Tests for `outages.aggregates.compute_aggregates`: count, total
downtime, longest single outage, and the hour-of-day/day-of-week
distribution of each outage's start time -- computed from confirmed
outage events only (outages requirement "Outage Aggregates").
"""

from __future__ import annotations

from ecoflow_stats.outages.aggregates import compute_aggregates
from ecoflow_stats.outages.resolve import EffectiveOutage, resolve
from tests.unit.outages.test_resolve import _legacy

_UTC = "UTC"


def _outage(start_ts: int, end_ts: int | None, **overrides: object) -> EffectiveOutage:
    defaults: dict[str, object] = {
        "kind": "outage",
        "source": "detected",
        "start_uncertain": False,
        "end_uncertain": False,
        "soc_start": None,
        "soc_end": None,
    }
    defaults.update(overrides)
    return EffectiveOutage(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def test_aggregates_reflect_confirmed_events() -> None:
    """Outages: "Aggregates reflect confirmed events" -- count, total
    downtime and longest single outage must match the given events
    exactly."""
    outages = [_outage(0, 600), _outage(1_000, 1_100)]  # 600s, 100s

    result = compute_aggregates(outages=outages, range_start=0, range_end=10_000, tz=_UTC)

    assert result.count == 2
    assert result.total_downtime_s == 700
    assert result.longest_s == 600


def test_no_outages_gives_empty_zeroed_aggregates() -> None:
    """Pass-1: an aggregates computation over an empty period must not
    blow up (division by zero on a mean) or report a fabricated
    longest/total."""
    result = compute_aggregates(outages=[], range_start=0, range_end=10_000, tz=_UTC)

    assert result.count == 0
    assert result.total_downtime_s == 0
    assert result.longest_s is None


def test_an_ongoing_outage_is_clipped_at_the_range_end() -> None:
    """An outage with no `end_ts` yet (still ongoing) must count only up
    to `range_end`, never past it -- otherwise "today's" downtime total
    would include time that has not happened yet."""
    outages = [_outage(9_000, None)]

    result = compute_aggregates(outages=outages, range_start=0, range_end=10_000, tz=_UTC)

    assert result.total_downtime_s == 1_000


def test_an_outage_entirely_before_the_range_is_excluded() -> None:
    outages = [_outage(0, 100)]

    result = compute_aggregates(outages=outages, range_start=500, range_end=10_000, tz=_UTC)

    assert result.count == 0
    assert result.total_downtime_s == 0


def test_an_outage_is_clipped_to_the_range_start() -> None:
    """An outage that began before the selected range but ends inside it
    must only count the portion inside the range."""
    outages = [_outage(0, 600)]

    result = compute_aggregates(outages=outages, range_start=400, range_end=10_000, tz=_UTC)

    assert result.total_downtime_s == 200


def test_the_heatmap_is_a_joint_weekday_by_hour_matrix_not_independent_marginals() -> None:
    """DATA-03 (qa-report-data-01.md): the heatmap used to be two
    independent 1D marginals (hour-of-day, day-of-week), which can
    never show a real pattern like "always on Monday afternoons" --
    only that outages happen on Mondays sometimes and at 14:00
    sometimes, with no way to tell whether it was the same outages.
    Pass-2: an implementation that faked a joint matrix by taking the
    outer product of the two marginals (the same values the old fields
    held) would wrongly populate every (weekday, hour) combination the
    marginals could independently produce -- here, (Monday, 18h) and
    (Tuesday, 14h) -- even though no outage ever started there."""
    monday_14_30_utc = 1767623400  # 2026-01-05T14:30:00Z, Monday
    tuesday_18_00_utc = 1767722400  # 2026-01-06T18:00:00Z, Tuesday
    outages = [
        _outage(monday_14_30_utc, monday_14_30_utc + 60),
        _outage(tuesday_18_00_utc, tuesday_18_00_utc + 60),
    ]

    result = compute_aggregates(
        outages=outages, range_start=0, range_end=tuesday_18_00_utc + 3_600, tz=_UTC
    )

    assert result.heatmap[0][14] == 1  # Monday 14:00
    assert result.heatmap[1][18] == 1  # Tuesday 18:00
    assert result.heatmap[0][18] == 0  # never Monday 18:00
    assert result.heatmap[1][14] == 0  # never Tuesday 14:00
    assert sum(sum(row) for row in result.heatmap) == 2


def test_an_unconfirmed_suspected_phantom_is_excluded_by_never_reaching_aggregates() -> None:
    """Outages: "An unconfirmed suspected phantom is excluded until
    confirmed." `compute_aggregates` has no phantom concept of its own
    -- it trusts its caller (`resolve()`) to have already excluded one,
    which this proves end-to-end: feeding exactly `resolve()`'s own
    `view.outages` for an unconfirmed phantom yields zero aggregates."""
    legacy_entry = _legacy(1, 100, 200, flags=frozenset({"suspected_phantom"}))

    view = resolve(detected=[], gaps=[], legacy=[legacy_entry], decisions=[], range_end=10_000)

    result = compute_aggregates(outages=view.outages, range_start=0, range_end=10_000, tz=_UTC)

    assert result.count == 0
