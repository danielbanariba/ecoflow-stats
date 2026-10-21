"""Pure AC-load series: one average/peak watt point per fixed-width
bucket across a whole requested range, shared by the JSON API
(`web.routes.api`'s `power/series` route) and the overview page's no-JS
fallback table (`web.views`).

Pure domain logic, no I/O: the caller reads the samples and chooses the
bucket width; this only groups and aggregates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ecoflow_stats.devices.reading import Reading


@dataclass(frozen=True, slots=True)
class AcLoadPoint:
    """One bucket's AC-load average and peak, in watts. Both are `None`
    when the bucket holds no sample that reported `ac_out_w` -- an
    explicit gap for a chart to break on, never a fabricated `0`."""

    ts: int
    ac_out_w_avg: float | None
    ac_out_w_max: float | None


def ac_load_series(
    samples: Iterable[tuple[int, Reading]],
    *,
    range_start: int,
    range_end: int,
    bucket_width_s: int,
) -> list[AcLoadPoint]:
    """One `AcLoadPoint` for every ``bucket_width_s`` window of
    ``[range_start, range_end)``, in time order -- including a window
    with no sample at all, so a time-ordered consumer always sees an
    explicit null point where the data has a hole (the same rule
    `grid/series` follows for an outage bucket).

    Buckets are anchored at ``range_start``, the same convention the
    `grid/series` route uses. A sample whose ``ac_out_w`` is `None` is
    skipped rather than counted as 0 W, so it neither drags the average
    down nor turns an unreported bucket into a flat-zero reading. A
    sample outside the range (the store's ``between`` is inclusive on
    both ends, so one can sit exactly at ``range_end``) is ignored.
    """
    watts_by_bucket: dict[int, list[float]] = {}
    for ts, reading in samples:
        if reading.ac_out_w is None:
            continue
        bucket_start = range_start + ((ts - range_start) // bucket_width_s) * bucket_width_s
        watts_by_bucket.setdefault(bucket_start, []).append(reading.ac_out_w)
    points: list[AcLoadPoint] = []
    for bucket_start in range(range_start, range_end, bucket_width_s):
        watts = watts_by_bucket.get(bucket_start)
        if not watts:
            points.append(AcLoadPoint(ts=bucket_start, ac_out_w_avg=None, ac_out_w_max=None))
            continue
        points.append(
            AcLoadPoint(
                ts=bucket_start, ac_out_w_avg=sum(watts) / len(watts), ac_out_w_max=max(watts)
            )
        )
    return points


__all__ = ["AcLoadPoint", "ac_load_series"]
