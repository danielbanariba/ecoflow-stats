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


def test_hour_of_day_and_day_of_week_reflect_each_events_start_time() -> None:
    """Outages: hour-of-day and day-of-week distributions reflect each
    event's start time. 2026-01-05 is a Monday; 14:30 UTC start."""
    monday_14_30_utc = 1767623400  # 2026-01-05T14:30:00Z
    outages = [_outage(monday_14_30_utc, monday_14_30_utc + 60)]

    result = compute_aggregates(
        outages=outages, range_start=0, range_end=monday_14_30_utc + 3_600, tz=_UTC
    )

    assert result.hour_of_day[14] == 1
    assert sum(result.hour_of_day) == 1
    assert result.day_of_week[0] == 1  # Monday == weekday() 0
    assert sum(result.day_of_week) == 1


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
