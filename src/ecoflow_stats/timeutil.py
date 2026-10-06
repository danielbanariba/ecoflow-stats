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


def relative_time_unit(age_s: int) -> tuple[str, int]:
    """Bucket a device sample's age in seconds into the coarsest
    human-friendly unit and count for an "X ago" display (UI-01,
    qa-report-ui-01.md: the overview showed the raw sample age in
    seconds, e.g. ``"8090 s"``, instead of a human-readable "2 hours
    ago").

    Returns an i18n key prefix (``"time.seconds_ago"``,
    ``"time.minutes_ago"``, ``"time.hours_ago"``, or
    ``"time.days_ago"``) paired with its count, ready for
    ``web.i18n.t(key, count=count)``'s existing singular/plural lookup.
    Floors rather than rounds, so the displayed age never overstates how
    stale the data actually is. A negative age (clock skew between the
    collector and web processes) clamps to zero rather than rendering a
    nonsensical negative count.
    """
    age_s = max(0, age_s)
    if age_s < 60:
        return "time.seconds_ago", age_s
    minutes = age_s // 60
    if minutes < 60:
        return "time.minutes_ago", minutes
    hours = minutes // 60
    if hours < 24:
        return "time.hours_ago", hours
    return "time.days_ago", hours // 24


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


__all__ = ["day_bounds", "local_day", "relative_time_unit", "split_by_local_days"]
