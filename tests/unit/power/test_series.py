"""Unit tests for the pure AC-load series bucketing (`power.series`).

No fake or mock is needed: `ac_load_series` only reads `ts` and
`Reading.ac_out_w` off plain in-memory tuples.
"""

from __future__ import annotations

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.power.series import ac_load_series


def _load(ac_out_w: float | None) -> Reading:
    return Reading(ac_out_w=ac_out_w)


def test_a_bucket_reports_the_average_and_peak_of_its_samples() -> None:
    """Pass-1: catches wrong avg/max arithmetic (the last value, a sum,
    or the max reported as the average), and a bucket boundary that
    leaks the first sample of the next window into this one. Pass-2:
    changing `max(watts)` to `watts[-1]` turns this red."""
    samples = [
        (1_000, _load(100.0)),
        (1_030, _load(400.0)),
        (1_059, _load(220.0)),
        (1_060, _load(50.0)),  # first sample of the second bucket
    ]

    points = ac_load_series(samples, range_start=1_000, range_end=1_120, bucket_width_s=60)

    assert [(p.ts, p.ac_out_w_avg, p.ac_out_w_max) for p in points] == [
        (1_000, 240.0, 400.0),
        (1_060, 50.0, 50.0),
    ]


def test_every_bucket_in_the_range_gets_a_point_with_explicit_nulls_for_gaps() -> None:
    """Pass-1: catches the series skipping a bucket with no sample,
    which leaves the time-ordered array with a silent hole so a chart
    with `connectNulls: false` has nothing to break on and draws a
    straight line across the gap (the class of defect `grid/series`
    already fixed once). Pass-2: building the points from the buckets
    that have samples instead of from the whole range turns this red."""
    samples = [(1_000, _load(100.0)), (1_130, _load(300.0))]

    points = ac_load_series(samples, range_start=1_000, range_end=1_180, bucket_width_s=60)

    assert [p.ts for p in points] == [1_000, 1_060, 1_120]
    assert (points[1].ac_out_w_avg, points[1].ac_out_w_max) == (None, None)
    assert points[2].ac_out_w_avg == 300.0


def test_samples_without_an_ac_out_w_reading_never_count_as_zero_watts() -> None:
    """Pass-1: catches a missing reading being coerced to `0` (a bucket
    of only unreported samples would chart as a real 0 W dip) or being
    counted in the average's divisor (100 W and 300 W plus one unreported
    sample would average 133.3 W instead of 200 W). Pass-2: replacing
    the `is None` skip with `or 0.0` turns this red."""
    samples = [
        (1_000, _load(100.0)),
        (1_020, _load(None)),
        (1_040, _load(300.0)),
        (1_060, _load(None)),  # second bucket holds only an unreported sample
    ]

    points = ac_load_series(samples, range_start=1_000, range_end=1_120, bucket_width_s=60)

    assert (points[0].ac_out_w_avg, points[0].ac_out_w_max) == (200.0, 300.0)
    assert (points[1].ac_out_w_avg, points[1].ac_out_w_max) == (None, None)


def test_a_sample_outside_the_range_does_not_leak_into_the_series() -> None:
    """Pass-1: `SampleStore.between` is inclusive on both ends, so the
    route hands this helper a sample stamped exactly at `range_end`;
    catches it opening an extra trailing bucket (or raising) instead of
    being left out, and a sample before the start being folded into the
    first bucket. Pass-2: appending a point per distinct sample bucket
    instead of walking `range(range_start, range_end, ...)` turns this
    red."""
    samples = [
        (940, _load(900.0)),  # before range_start
        (1_000, _load(100.0)),
        (1_120, _load(900.0)),  # exactly range_end
    ]

    points = ac_load_series(samples, range_start=1_000, range_end=1_120, bucket_width_s=60)

    assert [(p.ts, p.ac_out_w_max) for p in points] == [(1_000, 100.0), (1_060, None)]
