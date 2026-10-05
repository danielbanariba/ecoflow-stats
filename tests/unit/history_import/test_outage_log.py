"""Unit tests for the pure `outages.log` parser.

The legacy watcher (`ecoflow-power-watch`) appends one tab-separated line per
transition: `<naive local timestamp>\tcorte|retorno\t<soc>\t<minutes-or-empty>`.
This parser never guesses the timestamp's timezone — history-import requires
it as an explicit input (`source_tz`) — and never drops a line it cannot
pair or parse; it flags what it cannot resolve instead.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from ecoflow_stats.history_import.outage_log import parse_outage_log

_FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "legacy"


def _epoch(local_str: str, tz_name: str) -> int:
    """Independently compute the expected UTC epoch for a naive local
    timestamp — an oracle built directly from the stdlib, not from the
    parser under test."""
    return int(
        dt.datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=ZoneInfo(tz_name))
        .timestamp()
    )


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def test_a_corte_retorno_pair_parses_with_the_explicit_source_timezone() -> None:
    lines = [
        "2024-01-10 08:15:00\tcorte\t73\t",
        "2024-01-10 08:16:00\tretorno\t70\t1",
    ]
    result = parse_outage_log(lines, source_tz="America/Tegucigalpa")
    assert result.malformed == ()
    assert len(result.events) == 1
    event = result.events[0]
    assert event.start_ts == _epoch("2024-01-10 08:15:00", "America/Tegucigalpa")
    assert event.end_ts == _epoch("2024-01-10 08:16:00", "America/Tegucigalpa")
    assert event.soc_start == 73
    assert event.soc_end == 70
    assert event.logged_minutes == 1
    assert event.flags == frozenset()


def test_a_zero_charge_outage_start_is_flagged_suspected_phantom() -> None:
    result = parse_outage_log(_lines(_FIXTURES / "phantom.log"), source_tz="America/Tegucigalpa")
    phantom = next(e for e in result.events if e.soc_start == 0)
    assert "suspected_phantom" in phantom.flags


def test_a_non_zero_charge_outage_start_imports_without_the_phantom_flag() -> None:
    result = parse_outage_log(_lines(_FIXTURES / "phantom.log"), source_tz="America/Tegucigalpa")
    normal = next(e for e in result.events if e.soc_start == 73)
    assert "suspected_phantom" not in normal.flags


def test_an_unmatched_corte_is_flagged_orphan_start() -> None:
    """A `corte` superseded by a second `corte` before any `retorno`
    arrived must still be recorded — with no end — rather than silently
    disappearing when the next outage opens."""
    result = parse_outage_log(_lines(_FIXTURES / "orphans.log"), source_tz="America/Tegucigalpa")
    orphan_start = next(e for e in result.events if e.soc_start == 80)
    assert orphan_start.end_ts is None
    assert orphan_start.flags == frozenset({"orphan_start"})


def test_an_unmatched_retorno_is_flagged_orphan_end_and_backdated_using_minutes() -> None:
    """A `retorno` with no open `corte` (for example, the matching `corte`
    fell outside the imported range) must still be recorded, with its
    start backdated from the logged duration rather than collapsed to a
    zero-length event."""
    result = parse_outage_log(_lines(_FIXTURES / "orphans.log"), source_tz="America/Tegucigalpa")
    orphan_end = next(e for e in result.events if e.soc_end == 90)
    assert orphan_end.flags == frozenset({"orphan_end"})
    assert orphan_end.soc_start is None
    assert orphan_end.start_ts == orphan_end.end_ts - 20 * 60


def test_a_fall_back_fold_is_flagged_ambiguous_time() -> None:
    """A naive local timestamp that occurred twice (DST fall-back) must be
    flagged for review rather than silently resolved to an arbitrary one
    of its two possible instants."""
    result = parse_outage_log(_lines(_FIXTURES / "dst_zone.log"), source_tz="America/Chicago")
    fall_back_event = next(e for e in result.events if e.soc_start == 60)
    assert fall_back_event.flags == frozenset({"ambiguous_time"})


def test_a_spring_forward_gap_is_flagged_nonexistent_time() -> None:
    """A naive local timestamp that never occurred (DST spring-forward
    gap) must be flagged distinctly from an ambiguous one — conflating the
    two would tell a reviewer the wrong kind of problem to look for."""
    result = parse_outage_log(_lines(_FIXTURES / "dst_zone.log"), source_tz="America/Chicago")
    spring_forward_event = next(e for e in result.events if e.soc_start == 45)
    assert spring_forward_event.flags == frozenset({"nonexistent_time"})


def test_a_malformed_line_is_skipped_counted_and_named_with_a_reason() -> None:
    """One bad historical line must not abort the whole import, and must
    not vanish without a trace either — it is counted and named so an
    operator can audit exactly what was skipped and why."""
    lines = [
        "2024-03-01 00:00:00\tcorte\t50\t",  # 1: good (opens)
        "not even close to a valid line",  # 2: malformed - too few fields
        "2024-03-01 00:05:00\tretorno\t49\t5",  # 3: good (closes line 1)
        "2024-99-99 00:00:00\tcorte\t10\t",  # 4: malformed - invalid timestamp
        "2024-03-02 00:00:00\tfoo\t10\t",  # 5: malformed - unknown kind
        "2024-03-02 00:05:00\tcorte\tnot-a-number\t",  # 6: malformed - invalid charge value
    ]
    result = parse_outage_log(lines, source_tz="America/Tegucigalpa")
    assert len(result.events) == 1
    assert [m.line_no for m in result.malformed] == [2, 4, 5, 6]
    assert all(m.reason for m in result.malformed)
