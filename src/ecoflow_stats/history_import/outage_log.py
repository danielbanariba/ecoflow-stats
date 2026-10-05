"""Pure parser for the legacy `outages.log` format.

The legacy watcher (`ecoflow-power-watch`) appends one tab-separated line per
transition: a naive local timestamp, `corte` or `retorno`, the state of
charge, and — for `retorno` only — the outage's logged duration in minutes.
This module never guesses that timestamp's timezone (history-import
requirement: "Import Requires Explicit Serial and Source Timezone") and
never silently drops a line it cannot parse or pair; everything either
becomes an event or is counted as malformed with a reason. It writes
nothing — `storage/legacy.py` persists what this returns.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

_VALID_KINDS = ("corte", "retorno")


@dataclass(frozen=True, slots=True)
class MalformedLine:
    """One `outages.log` line that could not be parsed at all."""

    line_no: int
    reason: str
    raw: str


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """One outage event recovered from the legacy log, before storage.

    ``flags`` mirrors the `legacy_outages.flags` storage column: zero or
    more of ``suspected_phantom``, ``orphan_start``, ``orphan_end``,
    ``ambiguous_time``, ``nonexistent_time``.
    """

    start_ts: int
    end_ts: int | None
    soc_start: int | None
    soc_end: int | None
    logged_minutes: int | None
    start_line: str | None
    end_line: str | None
    flags: frozenset[str]


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Every event recovered from one `outages.log`, plus every line that
    could not be parsed at all."""

    events: tuple[ParsedEvent, ...]
    malformed: tuple[MalformedLine, ...]


@dataclass(frozen=True, slots=True)
class _ParsedLine:
    line_no: int
    raw: str
    ts: int
    kind: str
    soc: int
    minutes: int | None
    time_flags: frozenset[str]


def _resolve_local(naive: datetime, tz: ZoneInfo) -> tuple[datetime, frozenset[str]]:
    """Attach `tz` to a naive local timestamp, flagging a DST gap or fold.

    The two outcomes are kept mutually exclusive, even though both make
    `fold=0` and `fold=1` disagree on the UTC offset: a round-trip through
    UTC and back that fails to reproduce `naive` means the local time never
    existed (`nonexistent_time`, checked first); only once the round-trip
    succeeds does a fold-sensitive offset difference mean the local time
    genuinely occurred twice (`ambiguous_time`). Conflating the two would
    point a reviewer at the wrong kind of problem. Always resolves to
    `fold=0` (Python's default), so a flagged line still gets one
    deterministic, storable timestamp instead of being dropped.
    """
    local = naive.replace(tzinfo=tz, fold=0)
    alt = naive.replace(tzinfo=tz, fold=1)
    round_tripped = local.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_tripped != naive:
        return local, frozenset({"nonexistent_time"})
    if local.utcoffset() != alt.utcoffset():
        return local, frozenset({"ambiguous_time"})
    return local, frozenset()


def _parse_line(line_no: int, raw: str, tz: ZoneInfo) -> _ParsedLine | MalformedLine:
    fields = raw.split("\t")
    if len(fields) < 3:
        return MalformedLine(line_no, "expected at least 3 tab-separated fields", raw)
    timestamp_str, kind, soc_str, *rest = fields
    minutes_str = rest[0] if rest else ""

    try:
        # Deliberately naive here: `_resolve_local` below needs the naive
        # value itself to compute both the fold=0 and fold=1 resolutions
        # before this timestamp becomes timezone-aware.
        naive = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return MalformedLine(line_no, f"invalid timestamp {timestamp_str!r}", raw)

    if kind not in _VALID_KINDS:
        return MalformedLine(line_no, f"unknown event kind {kind!r}", raw)

    try:
        soc = int(soc_str)
    except ValueError:
        return MalformedLine(line_no, f"invalid charge value {soc_str!r}", raw)

    minutes: int | None = None
    if minutes_str:
        try:
            minutes = int(minutes_str)
        except ValueError:
            return MalformedLine(line_no, f"invalid duration value {minutes_str!r}", raw)

    local, time_flags = _resolve_local(naive, tz)
    return _ParsedLine(
        line_no=line_no,
        raw=raw,
        ts=int(local.timestamp()),
        kind=kind,
        soc=soc,
        minutes=minutes,
        time_flags=time_flags,
    )


@dataclass
class _OpenEvent:
    start_ts: int
    soc_start: int
    start_line: str
    flags: frozenset[str]

    def as_orphan(self) -> ParsedEvent:
        """Flush this still-open `corte` as an unmatched event: no `retorno`
        ever closed it, either because a later `corte` superseded it or
        because the log simply ended first."""
        return ParsedEvent(
            start_ts=self.start_ts,
            end_ts=None,
            soc_start=self.soc_start,
            soc_end=None,
            logged_minutes=None,
            start_line=self.start_line,
            end_line=None,
            flags=self.flags | {"orphan_start"},
        )


def parse_outage_log(lines: Iterable[str], source_tz: str) -> ParseResult:
    """Parse every line of a legacy `outages.log` into events, pairing each
    `corte` with the next `retorno` in file order.

    `source_tz` is required and never inferred from the data (history-import
    requirement). A line that cannot be parsed is skipped and counted in
    `ParseResult.malformed`, never raised — one bad historical line must not
    abort the whole import.
    """
    tz = ZoneInfo(source_tz)
    events: list[ParsedEvent] = []
    malformed: list[MalformedLine] = []
    open_event: _OpenEvent | None = None

    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.rstrip("\n")
        if not stripped.strip():
            continue

        parsed = _parse_line(line_no, stripped, tz)
        if isinstance(parsed, MalformedLine):
            malformed.append(parsed)
            continue

        if parsed.kind == "corte":
            if open_event is not None:
                events.append(open_event.as_orphan())
            flags = set(parsed.time_flags)
            if parsed.soc == 0:
                flags.add("suspected_phantom")
            open_event = _OpenEvent(
                start_ts=parsed.ts,
                soc_start=parsed.soc,
                start_line=parsed.raw,
                flags=frozenset(flags),
            )
            continue

        # kind == "retorno"
        if open_event is not None:
            events.append(
                ParsedEvent(
                    start_ts=open_event.start_ts,
                    end_ts=parsed.ts,
                    soc_start=open_event.soc_start,
                    soc_end=parsed.soc,
                    logged_minutes=parsed.minutes,
                    start_line=open_event.start_line,
                    end_line=parsed.raw,
                    flags=open_event.flags | parsed.time_flags,
                )
            )
            open_event = None
        else:
            start_ts = parsed.ts - parsed.minutes * 60 if parsed.minutes is not None else parsed.ts
            events.append(
                ParsedEvent(
                    start_ts=start_ts,
                    end_ts=parsed.ts,
                    soc_start=None,
                    soc_end=parsed.soc,
                    logged_minutes=parsed.minutes,
                    start_line=None,
                    end_line=parsed.raw,
                    flags=parsed.time_flags | {"orphan_end"},
                )
            )

    if open_event is not None:
        events.append(open_event.as_orphan())

    return ParseResult(events=tuple(events), malformed=tuple(malformed))


__all__ = ["MalformedLine", "ParseResult", "ParsedEvent", "parse_outage_log"]
