"""Pure, timezone-aware local-day bucketing and gap-aware proration.

A calendar day for the energy accounting engine (energy spec: "Day
Boundaries Use a Configurable Local Timezone") is defined by a configured
IANA timezone (``ECOFLOW_STATS_TZ``), not UTC, and read through
``zoneinfo`` rather than a fixed offset so a day correctly spans 23 or 25
hours across a DST transition (design-data section 4.5).

Pure: no I/O, no ``ecoflow_stats.config`` import. A caller supplies the
configured timezone string explicitly on every call, the same pattern
``rollups.service.derive_rollups``'s ``day_fn`` seam already established
for its battery-only placeholder, ``_utc_day`` -- this module's
``local_day`` is that seam's real implementation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def local_day(ts: int, tz: str) -> str:
    """The local calendar day (``'YYYY-MM-DD'``) that ``ts`` (a UTC epoch
    second) falls on under the IANA timezone ``tz`` -- never UTC's own
    day (scenario "A sample is attributed to its local calendar day, not
    its UTC day")."""
    return datetime.fromtimestamp(ts, tz=ZoneInfo(tz)).date().isoformat()


def day_bounds(day: str, tz: str) -> tuple[int, int]:
    """The half-open ``[start, end)`` UTC epoch-second interval covering
    local calendar day ``day`` (``'YYYY-MM-DD'``) under ``tz``.

    The interval's length is ``86_400`` s on a normal day, but
    ``82_800`` s (23 h) or ``90_000`` s (25 h) on a day containing a DST
    transition. ``zoneinfo`` resolves each boundary's own UTC offset
    independently of the other, so no special-casing is needed here: the
    offset used for ``end`` is recomputed for the next day's wall-clock
    time, not inherited from ``start``'s.
    """
    zone = ZoneInfo(tz)
    year, month, day_of_month = (int(part) for part in day.split("-"))
    start = datetime(year, month, day_of_month, tzinfo=zone)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def split_by_local_days(start_ts: int, end_ts: int, tz: str) -> list[tuple[str, float]]:
    """Split the half-open interval ``[start_ts, end_ts)`` into the local
    calendar days (under ``tz``) it overlaps, each paired with the
    fraction of the interval's total duration that falls on that day
    (the shares sum to ``1.0``) -- used to prorate a counter delta
    across the local days it spans, correctly on a 23- or 25-hour DST
    day (scenario "`split_by_local_days` prorates correctly across a DST
    transition").

    A zero-length interval (``start_ts == end_ts``, the very first
    sample with no predecessor to diff against) attributes its whole
    share to ``local_day(start_ts, tz)`` rather than dividing by zero.
    """
    total = end_ts - start_ts
    if total <= 0:
        return [(local_day(start_ts, tz), 1.0)]

    shares: list[tuple[str, float]] = []
    cursor = start_ts
    while cursor < end_ts:
        day = local_day(cursor, tz)
        _day_start, day_end = day_bounds(day, tz)
        segment_end = min(day_end, end_ts)
        shares.append((day, (segment_end - cursor) / total))
        cursor = segment_end
    return shares


__all__ = ["day_bounds", "local_day", "split_by_local_days"]
