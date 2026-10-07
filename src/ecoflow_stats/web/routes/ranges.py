"""Shared `from`/`to`-range validation for every route that accepts a
client-supplied timestamp (API2-01, qa-report-data-02.md).

Round 1's API-01 fix (`qa-report-data-01.md`) added this bound check
only inside `web.routes.api._resolve_range`, so `web.routes.pages` and
`web.routes.actions` each kept building their own unchecked
`range_start`/`range_end` -- `range_end = end if end is not None else
int(ctx.now().timestamp()); range_start = start if start is not None
else range_end - DEFAULT_S` -- with no bound check at all before
handing them to `SampleStore.between()` (a raw SQLite int64 bind) or
`timeutil.local_day()` (a platform `time_t` call). Both still raised
an unhandled `OverflowError` -> bare `500` through every page
(`/outages`, `/battery`, `/energy`, `/grid`) and every HTMX gap/legacy
review action -- the exact two crash sites API-01 had already fixed
for the JSON API, reachable through a strictly bigger blast radius.

Kept as its own tiny module, not inside `web.routes.api` (which every
one of `pages`/`actions` already imports from for other helpers): a
page or action route answers an invalid range in its own idiom (a
themed HTML error page, an HTML fragment) rather than `api`'s
`HTTPException`-flavored JSON body, so the one piece actually worth
sharing is the bound check and range-defaulting arithmetic itself,
raised as a plain `ValueError` each caller translates on its own
terms.
"""

from __future__ import annotations

from datetime import UTC, datetime

MIN_TS = int(datetime.min.replace(tzinfo=UTC).timestamp())
MAX_TS = int(datetime.max.replace(tzinfo=UTC).timestamp())
"""The representable-timestamp bound every `from`/`to` query/path/form
param is validated against (API-01/API2-01). Bounded to `datetime`'s
own min/max rather than SQLite's wider signed-int64 column range --
`datetime`'s range is the tighter of the two real crash sites this
bound must cover: `SampleStore.between()`'s raw SQLite bind (int64)
and `timeutil.local_day()`'s `datetime.fromtimestamp()` (platform
`time_t`, narrower still). Computed from a timezone-aware `datetime`,
whose `.timestamp()` is pure timedelta arithmetic against the epoch --
never a platform `mktime`/`gmtime` call -- so computing the bound
itself can never raise the same `OverflowError` it exists to
prevent."""


def validate_timestamp_bound(name: str, value: int | None) -> None:
    """Raise `ValueError` when `value` is outside `[MIN_TS, MAX_TS]`.
    `None` (an omitted query/form value) always passes -- the caller
    either substitutes its own default before this is reached
    (`resolve_range` below), or never defaults at all (e.g. `undo`'s
    `start` form field, always required).

    Also the one bound every other client-supplied integer ever handed
    to SQLite or `local_day()` outside an actual `from`/`to` pair must
    satisfy -- `gap_start`, a legacy entry's `start`, and a decision's
    `id` are never themselves timestamps spanning the full representable
    range, but reusing this same tight bound still rejects the huge
    values that would otherwise overflow a SQLite bind, and real values
    for any of them never come close to it."""
    if value is not None and not (MIN_TS <= value <= MAX_TS):
        raise ValueError(f"{name!r} is out of the representable timestamp range")


def resolve_range(
    *, now_ts: int, start: int | None, end: int | None, default_range_s: int
) -> tuple[int, int]:
    """Validate and default a `from`/`to` pair (API-01/API2-01): the one
    bound/ordering check every route accepting a client-supplied range
    applies before building `range_start`/`range_end`, so an
    out-of-range or inverted pair raises a `ValueError` before any
    downstream call can raise the unhandled `OverflowError` this exists
    to prevent. `default_range_s` stays the caller's own constant
    (each page/route keeps naming its own, even though every one of
    them is currently the same 7-day default) so one page's range can
    change independently later without an unrelated rename here."""
    validate_timestamp_bound("from", start)
    validate_timestamp_bound("to", end)
    range_end = end if end is not None else now_ts
    range_start = start if start is not None else range_end - default_range_s
    if range_start > range_end:
        raise ValueError("'from' must not be after 'to'")
    return range_start, range_end


__all__ = ["MAX_TS", "MIN_TS", "resolve_range", "validate_timestamp_bound"]
