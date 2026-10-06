"""Tests for `timeutil`: pure, timezone-aware local-day bucketing and
gap-aware proration for the energy accounting engine.

Energy spec: "Day Boundaries Use a Configurable Local Timezone"; design-data
section 4.5 ("Days come from `zoneinfo.ZoneInfo(ECOFLOW_STATS_TZ)`; DST days
are 23 or 25 hours").
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecoflow_stats.timeutil import (
    day_bounds,
    local_day,
    relative_time_unit,
    split_by_local_days,
)


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp())


def test_a_sample_is_attributed_to_its_local_calendar_day_not_its_utc_day() -> None:
    """Scenario "A sample is attributed to its local calendar day, not its
    UTC day": a defect that bucketed by UTC's own date (the
    `rollups.service._utc_day` placeholder this slice replaces) would
    silently shift a late-evening reading in a negative-offset timezone
    into the wrong day, undercounting yesterday's energy and
    overcounting today's."""
    ts = _ts(2026, 1, 2, 2, 0, 0)  # 2026-01-02 02:00 UTC == 2026-01-01 20:00 at UTC-6

    assert local_day(ts, "America/Tegucigalpa") == "2026-01-01"
    assert local_day(ts, "UTC") == "2026-01-02"


def test_changing_the_configured_timezone_changes_future_day_boundaries() -> None:
    """Scenario "Changing the configured timezone changes future day
    boundaries": a defect that resolved the day-bucketing rule once and
    cached it (rather than taking the configured timezone as an explicit
    argument on every call) would keep using the OLD timezone's
    boundaries after an operator changed `ECOFLOW_STATS_TZ`."""
    ts = _ts(2026, 1, 1, 23, 0, 0)

    assert local_day(ts, "UTC") == "2026-01-01"
    assert local_day(ts, "America/Tegucigalpa") == "2026-01-01"
    assert local_day(ts, "Pacific/Kiritimati") == "2026-01-02"  # UTC+14


def test_day_bounds_returns_the_half_open_utc_interval_for_a_normal_day() -> None:
    start, end = day_bounds("2026-01-01", "America/Tegucigalpa")

    assert end - start == 86_400
    assert start == _ts(2026, 1, 1, 6, 0, 0)  # 2026-01-01 00:00 -06:00 == 06:00 UTC


def test_split_by_local_days_prorates_across_a_spring_forward_23_hour_day() -> None:
    """Scenario "`split_by_local_days` prorates correctly across a DST
    transition (23/25-hour days)": America/Chicago's 2024-03-10 has only
    23 hours (02:00 CST jumps straight to 03:00 CDT). A defect assuming
    every day is a fixed 86_400 s would under-weight this day's actual
    share of an interval spanning into the next day, silently shifting
    energy into the wrong day."""
    start, day_end = day_bounds("2024-03-10", "America/Chicago")
    assert day_end - start == 23 * 3600

    shares = split_by_local_days(start, start + 23 * 3600 + 3600, "America/Chicago")

    assert shares == [
        ("2024-03-10", pytest.approx(23 * 3600 / (24 * 3600))),
        ("2024-03-11", pytest.approx(3600 / (24 * 3600))),
    ]
    assert sum(share for _day, share in shares) == pytest.approx(1.0)


def test_split_by_local_days_prorates_across_a_fall_back_25_hour_day() -> None:
    """The symmetric half of the DST test above: America/Chicago's
    2024-11-03 has 25 hours (02:00 CDT falls back to 01:00 CST). Proven
    independently rather than assuming the spring-forward case already
    covers both directions of a non-86_400 s day."""
    start, day_end = day_bounds("2024-11-03", "America/Chicago")
    assert day_end - start == 25 * 3600

    shares = split_by_local_days(start, start + 25 * 3600 + 3600, "America/Chicago")

    assert shares == [
        ("2024-11-03", pytest.approx(25 * 3600 / (26 * 3600))),
        ("2024-11-04", pytest.approx(3600 / (26 * 3600))),
    ]
    assert sum(share for _day, share in shares) == pytest.approx(1.0)


def test_split_by_local_days_attributes_a_zero_length_interval_to_one_day() -> None:
    """A degenerate single-instant interval (the very first sample, with
    no predecessor to diff against) must not divide by zero or vanish
    from the result -- it belongs entirely to its own local day."""
    ts = _ts(2026, 1, 1, 12, 0, 0)

    assert split_by_local_days(ts, ts, "UTC") == [("2026-01-01", 1.0)]


def test_relative_time_unit_buckets_seconds_to_the_matching_i18n_key_and_count() -> None:
    """UI-01 (qa-report-ui-01.md): the overview rendered the device's raw
    sample age in seconds (e.g. "8090 s") instead of a human relative
    time like "2 hours ago". A defect that left the raw seconds
    unconverted, or picked the wrong unit boundary (e.g. switching to
    minutes only past 120 s, or to hours only past 7200 s), would
    resurface the same complaint or silently misreport the staleness."""
    assert relative_time_unit(45) == ("time.seconds_ago", 45)
    assert relative_time_unit(60) == ("time.minutes_ago", 1)
    assert relative_time_unit(125) == ("time.minutes_ago", 2)
    assert relative_time_unit(3600) == ("time.hours_ago", 1)
    assert relative_time_unit(8090) == ("time.hours_ago", 2)
    assert relative_time_unit(86_400) == ("time.days_ago", 1)


def test_relative_time_unit_floors_rather_than_rounds_up_a_partial_unit() -> None:
    """A defect that rounded 119 elapsed minutes up to "2 hours ago"
    (instead of flooring to "1 hour ago") would overstate how stale the
    data actually is -- a staleness indicator must never claim data is
    older than it is."""
    assert relative_time_unit(119 * 60) == ("time.hours_ago", 1)


def test_relative_time_unit_clamps_a_negative_age_to_zero() -> None:
    """Clock skew between the collector process and the web process
    could produce a negative age; a defect that let it through
    unclamped would render a nonsensical negative count instead of "0 s
    ago"."""
    assert relative_time_unit(-5) == ("time.seconds_ago", 0)
