"""Unit tests for the pure overview-page and outages-page view models
(web-ui "One Device at a Time, With a Selector" and "Empty and Stale
States Are Shown Explicitly"; amendment items 9 and 10; Phase 15 PR ii's
outages-page summary, events table, and mains-strip fallback data).

Kept separate from the route: every builder here takes already-fetched
plain values (a ``DeviceStatus``, device records, reconciled outages and
gaps) with no FastAPI, Jinja2, or storage dependency, so "what does this
page show" is provable with plain dataclasses.
"""

from __future__ import annotations

from itertools import pairwise

from ecoflow_stats.battery.stats import BatteryPowerStatus
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.live_status.status import DeviceStatus, OutageStatus
from ecoflow_stats.outages.aggregates import OutageAggregates
from ecoflow_stats.outages.model import Gap
from ecoflow_stats.outages.resolve import EffectiveOutage
from ecoflow_stats.storage.devices import DeviceRecord
from ecoflow_stats.web.views import (
    OutagesSummary,
    build_device_options,
    build_mains_strip_segments,
    build_outage_event_rows,
    build_outages_summary,
    build_outages_view_model,
    build_overview_view_model,
    format_local_dt,
)

_NO_OUTAGE = OutageStatus(ongoing=False, since=None)
_NO_OUTAGES_30D = OutagesSummary(
    count=0, total_downtime_s=0, longest_s=None, mean_s=None, brief_count=0, unknown_time_s=0
)
_RANGE_START = 1_000_000
_RANGE_END = 1_000_000 + 7 * 24 * 60 * 60


def _outage(
    start_ts: int, end_ts: int | None, *, source: str = "detected", **overrides: object
) -> EffectiveOutage:
    defaults: dict[str, object] = {
        "kind": "outage",
        "start_uncertain": False,
        "end_uncertain": False,
        "soc_start": None,
        "soc_end": None,
        "legacy_id": None,
    }
    defaults.update(overrides)
    return EffectiveOutage(start_ts=start_ts, end_ts=end_ts, source=source, **defaults)  # type: ignore[arg-type]


def _gap(start_ts: int, end_ts: int | None, **overrides: object) -> Gap:
    defaults: dict[str, object] = {
        "cause": "unknown",
        "state_before": "present",
        "state_after": "present",
        "soc_before": None,
        "soc_after": None,
        "chg_ac_wh_delta": None,
        "expected_in_wh": None,
        "evidence": "inconclusive",
        "failures": {},
    }
    defaults.update(overrides)
    return Gap(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _device(device_id: int, sn: str, name: str | None = None) -> DeviceRecord:
    return DeviceRecord(
        id=device_id,
        sn=sn,
        name=name,
        product_name=None,
        adapter_id="delta_pro",
        online=None,
        online_checked_at=None,
        created_at=0,
    )


def test_two_devices_produce_two_selector_options_with_the_selected_one_marked() -> None:
    """Scenario "Multiple devices show one at a time with a selector
    offered": the selector must list every configured device, not only
    the one currently shown."""
    records = (_device(1, "BA31ZEB1SF7F0001"), _device(2, "BA31ZEB1SF7F0002"))

    options = build_device_options(records, selected_device_id=2)

    assert [opt.id for opt in options] == [1, 2]
    assert [opt.selected for opt in options] == [False, True]


def test_a_named_device_uses_its_configured_name_not_the_masked_serial() -> None:
    records = (_device(1, "BA31ZEB1SF7F0001", name="Garage"),)

    options = build_device_options(records, selected_device_id=1)

    assert options[0].label == "Garage"


def test_an_unnamed_device_falls_back_to_its_masked_serial() -> None:
    """Pass-2 target: using the full serial here would leak it onto a
    page a LAN visitor without a password can already reach."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)

    options = build_device_options(records, selected_device_id=1)

    assert options[0].label == "…0001"


def test_a_device_with_no_recorded_sample_reports_no_data_not_a_fabricated_reading() -> None:
    """Scenario "A device with no data shows an explicit empty state":
    `has_data` must come from `status.ts`, never from a zero-valued
    reading field."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=None,
        age_s=None,
        stale=False,
        grid="unknown",
        reading=None,
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
    )

    assert view.has_data is False


def test_a_fresh_sample_reports_data_with_its_grid_state_and_charge() -> None:
    """Pass-2 target: swapping `status.grid` for a hardcoded `"present"`
    would still pass a has-data-only test; this asserts the actual
    propagated value."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=1000,
        age_s=12,
        stale=False,
        grid="absent",
        reading=Reading(soc=42),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
    )

    assert view.has_data is True
    assert view.grid == "absent"
    assert view.soc == 42
    assert view.stale is False
    assert view.age_s == 12


def test_a_stale_sample_is_reported_stale_alongside_its_age() -> None:
    """Scenario "Stale data is marked, not shown as current"."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=1000,
        age_s=9000,
        stale=True,
        grid="present",
        reading=Reading(soc=10),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
    )

    assert view.stale is True
    assert view.age_s == 9000


def test_a_fresh_sample_reports_power_flows_battery_health_and_last_update() -> None:
    """UI-04/UI-05 (qa-report-ui-01.md): the overview's bento tiles were
    mostly empty even though every one of these values already exists
    on the latest sample. A defect that dropped a field during the
    reshape, or read it from the wrong `Reading` attribute, would
    starve a tile of data it could have shown."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=5000,
        age_s=12,
        stale=False,
        grid="present",
        reading=Reading(
            soc=80,
            solar_in_w=300.0,
            ac_in_w=0.0,
            ac_out_w=150.0,
            batt_in_w=150.0,
            batt_out_w=0.0,
            soh=97.5,
            cycles=42,
            chg_remain_min=90,
        ),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
    )

    assert view.last_update_ts == 5000
    assert view.solar_in_w == 300.0
    assert view.ac_in_w == 0.0
    assert view.ac_out_w == 150.0
    assert view.soh == 97.5
    assert view.cycles == 42
    assert view.battery_power == BatteryPowerStatus(
        direction="charging", net_w=150.0, remaining_min=90
    )


def test_a_device_with_no_recorded_sample_reports_every_new_field_as_unavailable() -> None:
    """Named Defect "missing read as zero": with no sample at all, every
    one of the new power-flow/battery-health fields must be `None`, not
    a fabricated `0` or an empty-but-truthy `BatteryPowerStatus`."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=None,
        age_s=None,
        stale=False,
        grid="unknown",
        reading=None,
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
    )

    assert view.last_update_ts is None
    assert view.solar_in_w is None
    assert view.ac_in_w is None
    assert view.ac_out_w is None
    assert view.soh is None
    assert view.cycles is None
    assert view.battery_power is None


def test_the_30_day_outage_summary_is_propagated_from_the_caller_unmodified() -> None:
    """UI-04/UI-05: the overview's outage tiles (count, longest outage,
    time on battery) must reflect the real pre-computed 30-day summary
    the route builds through the same `outages.aggregates` pipeline the
    outages page already uses -- a defect that dropped or recomputed it
    here would show a stale or fabricated figure."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=None,
        age_s=None,
        stale=False,
        grid="unknown",
        reading=None,
        outage=_NO_OUTAGE,
    )
    summary = OutagesSummary(
        count=3, total_downtime_s=900, longest_s=500, mean_s=300, brief_count=1, unknown_time_s=20
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=summary,
    )

    assert view.outages_30d == summary


def test_outages_summary_reports_count_total_longest_mean_brief_and_unknown_time() -> None:
    """Pass-1: a summary that drops the mean, the brief count, or the
    unknown-time total would silently under-report the outages page's
    headline numbers (task 15.5 scenario 1)."""
    aggregates = OutageAggregates(count=2, total_downtime_s=600, longest_s=400)
    briefs = [_outage(10, 11, kind="brief"), _outage(20, 21, kind="brief")]
    gaps = [_gap(_RANGE_START + 100, _RANGE_START + 400)]

    summary = build_outages_summary(
        aggregates=aggregates,
        briefs=briefs,
        gaps=gaps,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
    )

    assert summary.count == 2
    assert summary.total_downtime_s == 600
    assert summary.longest_s == 400
    assert summary.mean_s == 300
    assert summary.brief_count == 2
    assert summary.unknown_time_s == 300


def test_outages_summary_reports_mean_and_longest_as_unavailable_with_no_outages() -> None:
    """Pass-1: dividing by a zero count must never crash, and a 0-second
    mean would dishonestly imply a real measured zero duration rather
    than "no outages yet"."""
    aggregates = OutageAggregates(count=0, total_downtime_s=0, longest_s=None)

    summary = build_outages_summary(
        aggregates=aggregates, briefs=[], gaps=[], range_start=_RANGE_START, range_end=_RANGE_END
    )

    assert summary.mean_s is None
    assert summary.longest_s is None
    assert summary.unknown_time_s == 0


def test_outage_event_rows_are_sorted_most_recent_first_with_uncertainty_and_source() -> None:
    """Pass-1: showing events oldest-first, or dropping the uncertainty/
    source fields, would defeat the events table's purpose (task 15.5
    scenario 3 -- "each event's start/end, its uncertainty, and its
    source")."""
    older = _outage(100, 200, source="legacy", start_uncertain=True, end_uncertain=True)
    newer = _outage(500, 600, source="detected", start_uncertain=False, end_uncertain=False)

    rows = build_outage_event_rows([older, newer])

    assert [row.start_ts for row in rows] == [500, 100]
    assert rows[0].source == "detected"
    assert rows[0].start_uncertain is False
    assert rows[1].source == "legacy"
    assert rows[1].start_uncertain is True


def test_mains_strip_segments_cover_the_full_range_with_no_gaps_or_overlaps() -> None:
    """Pass-1: a hole or overlap in the run-length series would defeat
    the mains strip's entire purpose (same contract as the API route's
    own test in `test_api_outages.py`, reproduced here for the page's
    independent fallback-table builder)."""
    outages = [_outage(_RANGE_START + 1_000, _RANGE_START + 1_600)]
    gaps = [_gap(_RANGE_START + 10_000, _RANGE_START + 10_400)]

    segments = build_mains_strip_segments(
        outages=outages, gaps=gaps, range_start=_RANGE_START, range_end=_RANGE_END
    )

    assert segments[0].start_ts == _RANGE_START
    assert segments[-1].end_ts == _RANGE_END
    for prev, nxt in pairwise(segments):
        assert prev.end_ts == nxt.start_ts
    absent = [(s.start_ts, s.end_ts) for s in segments if s.state == "absent"]
    unknown = [(s.start_ts, s.end_ts) for s in segments if s.state == "unknown"]
    assert absent == [(_RANGE_START + 1_000, _RANGE_START + 1_600)]
    assert unknown == [(_RANGE_START + 10_000, _RANGE_START + 10_400)]


def test_mains_strip_segments_paint_a_confirmed_outage_over_a_raw_gap() -> None:
    """Pass-1: a gap the user has since confirmed as a real outage must
    render as a solid cut (absent), not hatched (unknown) -- the same
    priority-order rule `web.routes.api._build_mains_strip` already
    enforces (apply-progress-batch13 design decision #6); regressing the
    paint order here would make the page's fallback table disagree with
    its own chart's documented visual language."""
    start, end = _RANGE_START + 2_000, _RANGE_START + 2_400
    outages = [_outage(start, end, source="gap")]
    gaps = [_gap(start, end)]

    segments = build_mains_strip_segments(
        outages=outages, gaps=gaps, range_start=_RANGE_START, range_end=_RANGE_END
    )

    absent = [s for s in segments if s.state == "absent"]
    unknown = [s for s in segments if s.state == "unknown"]
    assert absent == [type(absent[0])(start_ts=start, end_ts=end, state="absent")]
    assert unknown == []


def test_mains_strip_segments_clip_an_ongoing_outage_at_range_end() -> None:
    """Pass-1: an open-ended outage (`end_ts=None`) must clip at
    `range_end`, never crash or leave the strip uncovered past it."""
    outages = [_outage(_RANGE_END - 500, None)]

    segments = build_mains_strip_segments(
        outages=outages, gaps=[], range_start=_RANGE_START, range_end=_RANGE_END
    )

    assert segments[-1] == type(segments[-1])(
        start_ts=_RANGE_END - 500, end_ts=_RANGE_END, state="absent"
    )


def test_format_local_dt_renders_a_numeric_local_time_string_in_the_configured_zone() -> None:
    """Pass-1/Pass-2: a formatter that ignores `tz` and always renders
    UTC would show the wrong wall-clock hour to an operator outside
    UTC -- flip target: the America/Tegucigalpa hour (UTC-6) must differ
    from the raw UTC hour for the same instant."""
    ts = 1_700_000_400  # 2023-11-14 22:20:00 UTC

    utc_rendered = format_local_dt(ts, "UTC")
    local_rendered = format_local_dt(ts, "America/Tegucigalpa")

    assert utc_rendered == "2023-11-14 22:20"
    assert local_rendered == "2023-11-14 16:20"


def test_build_outages_view_model_assembles_every_part_without_dropping_fields() -> None:
    """Pass-1: a wiring mistake (e.g. swapping the heatmap and summary,
    or forgetting the gap-review count) would silently serve the wrong
    number on the page; this proves every input reaches its own named
    field on the assembled view model."""
    records = (_device(7, "BA31ZEB1SF7F0007"),)
    aggregates = OutageAggregates(count=1, total_downtime_s=100, longest_s=100)
    outages = [_outage(_RANGE_START + 50, _RANGE_START + 150)]

    view = build_outages_view_model(
        device_records=records,
        selected_device_id=7,
        aggregates=aggregates,
        outages=outages,
        briefs=[],
        gaps=[],
        gap_review_count=3,
        legacy_review_count=5,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        mains_strip_src="/api/v1/mains-strip?device=7",
    )

    assert view.selected_device_id == 7
    assert view.devices[0].id == 7
    assert view.summary.count == 1
    assert view.heatmap.matrix == tuple(tuple(row) for row in aggregates.heatmap)
    assert view.events[0].start_ts == _RANGE_START + 50
    assert view.gap_review_count == 3
    assert view.legacy_review_count == 5
    assert view.mains_strip_src == "/api/v1/mains-strip?device=7"
    assert view.mains_strip[0].start_ts == _RANGE_START
