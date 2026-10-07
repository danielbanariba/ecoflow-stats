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

from datetime import UTC, datetime
from itertools import pairwise

from ecoflow_stats.battery.stats import BatteryPowerStatus
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.live_status.status import DeviceStatus, OutageStatus
from ecoflow_stats.outages.aggregates import OutageAggregates
from ecoflow_stats.outages.model import Gap
from ecoflow_stats.outages.resolve import EffectiveOutage
from ecoflow_stats.storage.devices import DeviceRecord
from ecoflow_stats.storage.rollups import DailyGridRollup
from ecoflow_stats.web.views import (
    OutagesSummary,
    build_device_options,
    build_energy_period_row,
    build_energy_view_model,
    build_grid_view_model,
    build_mains_strip_segments,
    build_outage_event_rows,
    build_outages_summary,
    build_outages_view_model,
    build_overview_view_model,
    format_compact_local_dt,
    format_duration,
    format_hours_duration,
    format_local_dt,
)

_NO_OUTAGE = OutageStatus(ongoing=False, since=None)
_NO_OUTAGES_30D = OutagesSummary(
    count=0, total_downtime_s=0, longest_s=None, mean_s=None, brief_count=0, unknown_time_s=0
)
_RANGE_START = 1_000_000
_RANGE_END = 1_000_000 + 7 * 24 * 60 * 60
_ALL_PERIODS_COMPLETE_NOW_TS = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
"""`now_ts` for `build_energy_view_model` tests that are not exercising
F2's in-progress-period exclusion: well after every `"2026-01-0N"`
period these tests seed, so every period is complete and `best_day`/
`worst_day` rank exactly as they did before that filter existed."""


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
        tz="UTC",
        now_ts=0,
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
        tz="UTC",
        now_ts=1000,
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
        tz="UTC",
        now_ts=1000,
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
        tz="UTC",
        now_ts=5000,
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


def _utc_ts(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp())


def test_last_update_is_today_compares_local_calendar_days_not_utc_ones() -> None:
    """C-02 (qa-report-ui-01.md), Pass-1/Pass-2: a defect that compared
    UTC calendar days instead of the configured timezone's local days
    would wrongly report "not today" here -- `now` and the sample fall
    on different UTC days (2026-01-02 vs 2026-01-01) but the SAME local
    day under America/Tegucigalpa (UTC-6): now is 2026-01-01 20:00
    local, the sample is 2026-01-01 17:00 local."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    now_ts = _utc_ts(2026, 1, 2, 2, 0, 0)  # 2026-01-01 20:00 at America/Tegucigalpa
    sample_ts = _utc_ts(2026, 1, 1, 23, 0, 0)  # 2026-01-01 17:00 at America/Tegucigalpa
    status = DeviceStatus(
        device_id=1,
        ts=sample_ts,
        age_s=0,
        stale=False,
        grid="present",
        reading=Reading(soc=50),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
        tz="America/Tegucigalpa",
        now_ts=now_ts,
    )

    assert view.last_update_is_today is True


def test_last_update_from_a_previous_local_day_is_reported_as_not_today() -> None:
    """Pass-2 target: proves this is not hardcoded `True` -- a sample
    from the local day before `now` must be reported as not today."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    now_ts = _utc_ts(2026, 1, 2, 2, 0, 0)  # 2026-01-01 20:00 at America/Tegucigalpa
    sample_ts = _utc_ts(2025, 12, 31, 23, 0, 0)  # 2025-12-31 17:00 at America/Tegucigalpa
    status = DeviceStatus(
        device_id=1,
        ts=sample_ts,
        age_s=0,
        stale=False,
        grid="present",
        reading=Reading(soc=50),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(
        device_records=records,
        selected_device_id=1,
        status=status,
        outages_30d=_NO_OUTAGES_30D,
        tz="America/Tegucigalpa",
        now_ts=now_ts,
    )

    assert view.last_update_is_today is False


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
        tz="UTC",
        now_ts=0,
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
        tz="UTC",
        now_ts=0,
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


def test_format_compact_local_dt_shows_just_the_time_when_not_told_to_show_the_date() -> None:
    """C-02 (qa-report-ui-01.md): the overview's "Last update" tile
    showed a full "YYYY-MM-DD HH:MM" (`format_local_dt`'s own shape)
    even though the sample is almost always from today -- this compact
    formatter must render a bare time instead when the caller says the
    date need not be shown."""
    ts = 1_700_000_400  # 2023-11-14 22:20:00 UTC == 16:20 at America/Tegucigalpa

    assert format_compact_local_dt(ts, "America/Tegucigalpa", show_date=False) == "16:20"


def test_format_compact_local_dt_prefixes_month_and_day_when_told_to_show_the_date() -> None:
    """Pass-2 target: a defect that always rendered the bare time
    regardless of `show_date` would make a reading from a previous day
    indistinguishable from one taken minutes ago."""
    ts = 1_700_000_400  # 2023-11-14 22:20:00 UTC == 16:20 at America/Tegucigalpa

    assert format_compact_local_dt(ts, "America/Tegucigalpa", show_date=True) == "11-14 16:20"


def _unit_catalog(key: str) -> str:
    return {
        "duration.unit.days": "{count} d",
        "duration.unit.hours": "{count} h",
        "duration.unit.minutes": "{count} min",
        "duration.unit.seconds": "{count} s",
    }[key]


def test_format_duration_joins_duration_parts_through_the_translator() -> None:
    """C-01 (qa-report-ui-01.md): the overview's "Longest outage"/"Time
    on battery" tiles and the outages page's summary showed a raw
    second count (e.g. "25200s") instead of a human duration. Pass-1/
    Pass-2: a defect that formatted the raw seconds directly (skipping
    `duration_parts`), or picked the wrong i18n key per unit, would
    fail these exact examples from the reported defect."""
    assert format_duration(25_200, _unit_catalog) == "7 h"
    assert format_duration(5_100, _unit_catalog) == "1 h 25 min"
    assert format_duration(183_600, _unit_catalog) == "2 d 3 h"
    assert format_duration(0, _unit_catalog) == "0 s"


def test_format_hours_duration_converts_to_seconds_through_format_duration() -> None:
    """C-01 followup (qa-report-ui-02.md): the battery page's observed-
    autonomy table was the one place left rendering a duration as
    decimal hours (`"14.0h"`) while every other duration on this app
    -- including this same page's own `format_duration` calls --
    already showed `"12 h 31 min"`. Pass-1/Pass-2: a defect that
    treated `hours` as already-seconds (skipping the `* 3600`), or
    truncated instead of rounding, would fail these exact
    conversions, including the two round-trip-lossy values
    (`6.1`, `6.7`) that only come out clean because of the
    conversion's own rounding."""
    assert format_hours_duration(2.0, _unit_catalog) == "2 h"
    assert format_hours_duration(0.0, _unit_catalog) == "0 s"
    assert format_hours_duration(6.1, _unit_catalog) == "6 h 6 min"
    assert format_hours_duration(6.7, _unit_catalog) == "6 h 42 min"


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


def _energy_period_dict(
    period: str,
    *,
    chg_ac_wh: float = 0.0,
    chg_dc_wh: float = 0.0,
    chg_solar_wh: float = 0.0,
    dsg_ac_wh: float = 0.0,
    dsg_dc_wh: float = 0.0,
    chg_ac_est_wh: float = 0.0,
    cost: float | str = "unavailable",
    currency: str = "",
    flags: list[str] | None = None,
) -> dict[str, object]:
    """Shaped exactly like `web.routes.api.build_energy_periods`'s own
    period dicts -- `build_energy_period_row` only ever reshapes that
    already-computed output, so these tests hand-build the same shape
    rather than re-deriving it from raw rollup rows."""
    return {
        "period": period,
        "chg_ac_wh": chg_ac_wh,
        "chg_dc_wh": chg_dc_wh,
        "chg_solar_wh": chg_solar_wh,
        "dsg_ac_wh": dsg_ac_wh,
        "dsg_dc_wh": dsg_dc_wh,
        "chg_ac_est_wh": chg_ac_est_wh,
        "cost": cost,
        "currency": currency,
        "flags": flags or [],
    }


def test_build_energy_period_row_reads_every_field_from_its_own_dict_key() -> None:
    """Pass-1: catches a wrong dict key being read for this row (e.g.
    swapping `chg_dc_wh`/`dsg_dc_wh`, or dropping `flags`), which would
    silently show the wrong source's energy on the page without ever
    raising."""
    row = build_energy_period_row(
        _energy_period_dict(
            "2026-01-08",
            chg_ac_wh=1.0,
            chg_dc_wh=2.0,
            chg_solar_wh=3.0,
            dsg_ac_wh=4.0,
            dsg_dc_wh=5.0,
            chg_ac_est_wh=6.0,
            cost=0.5,
            currency="USD",
            flags=["counter_reset"],
        )
    )
    assert row.period == "2026-01-08"
    assert row.chg_ac_wh == 1.0
    assert row.chg_dc_wh == 2.0
    assert row.chg_solar_wh == 3.0
    assert row.dsg_ac_wh == 4.0
    assert row.dsg_dc_wh == 5.0
    assert row.chg_ac_est_wh == 6.0
    assert row.cost == 0.5
    assert row.currency == "USD"
    assert row.flags == ("counter_reset",)


def test_build_energy_view_model_sums_in_and_out_across_every_period() -> None:
    """energy requirement "Daily Energy by Source and Direction": the
    summary cards' totals. Pass-1: catches summing the wrong fields
    (e.g. including `dsg_ac_wh` in `total_in_wh`) or forgetting a
    period, which would silently misreport the device's real energy
    flow."""
    periods = [
        _energy_period_dict(
            "2026-01-07", chg_ac_wh=1_000.0, chg_dc_wh=0.0, chg_solar_wh=500.0, dsg_ac_wh=200.0
        ),
        _energy_period_dict(
            "2026-01-08", chg_ac_wh=2_000.0, chg_dc_wh=100.0, chg_solar_wh=0.0, dsg_ac_wh=300.0
        ),
    ]
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=None,
        currency="",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=_ALL_PERIODS_COMPLETE_NOW_TS,
        tz="UTC",
    )
    assert view.total_in_wh == 1_000.0 + 500.0 + 2_000.0 + 100.0
    assert view.total_out_wh == 200.0 + 300.0


def test_build_energy_view_model_reports_cost_unavailable_without_a_tariff() -> None:
    """Energy scenario "No configured tariff shows energy without a
    fabricated cost". Pass-1: catches the total silently rendering as
    a fabricated `0` instead of the honest `"unavailable"` the spec
    requires when `tariff` is `None` -- the trap a naive `sum()` over
    each period's own `"unavailable"` string would fall into."""
    periods = [_energy_period_dict("2026-01-08", chg_ac_wh=5_000.0, cost="unavailable")]
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=None,
        currency="",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=_ALL_PERIODS_COMPLETE_NOW_TS,
        tz="UTC",
    )
    assert view.total_cost == "unavailable"
    assert view.best_day is None
    assert view.worst_day is None


def test_build_energy_view_model_ranks_best_and_worst_day_by_cost() -> None:
    """Resolved semantics (apply-progress-pages): best/worst day ranked
    by cost, lowest and highest. Pass-1: catches the ranking picking
    the wrong period (e.g. by raw Wh instead of cost, or reversing
    best/worst), which would recommend the wrong day as cheapest."""
    periods = [
        _energy_period_dict("2026-01-06", chg_ac_wh=1_000.0, cost=0.20, currency="USD"),
        _energy_period_dict("2026-01-07", chg_ac_wh=5_000.0, cost=1.00, currency="USD"),
        _energy_period_dict("2026-01-08", chg_ac_wh=2_000.0, cost=0.40, currency="USD"),
    ]
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=0.20,
        currency="USD",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=_ALL_PERIODS_COMPLETE_NOW_TS,
        tz="UTC",
    )
    assert view.total_cost == 1.60
    assert view.best_day is not None
    assert view.best_day.period == "2026-01-06"
    assert view.worst_day is not None
    assert view.worst_day.period == "2026-01-07"


def test_build_energy_view_model_excludes_the_in_progress_day_from_best_day() -> None:
    """F2 (orchestrator QA batch F): a few hours of cheap partial data
    from today must never win "cheapest period" over a genuinely
    complete, more expensive day. Pass-1: catches `best_day` ranking
    over every period including one whose local day has not finished
    yet. Pass-2: dropping the `complete_rows` filter in
    `build_energy_view_model` (ranking over all `rows` again) turns
    this red -- `best_day` would become the $0.001 in-progress day."""
    periods = [
        _energy_period_dict("2026-01-06", chg_ac_wh=5_000.0, cost=1.00, currency="USD"),
        _energy_period_dict("2026-01-07", chg_ac_wh=1_000.0, cost=0.20, currency="USD"),
        _energy_period_dict("2026-01-08", chg_ac_wh=50.0, cost=0.01, currency="USD"),
    ]
    now_ts = int(datetime(2026, 1, 8, 12, 0, tzinfo=UTC).timestamp())  # mid-day on the 8th
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=0.20,
        currency="USD",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=now_ts,
        tz="UTC",
    )
    assert view.best_day is not None
    assert view.best_day.period == "2026-01-07"


def test_build_energy_view_model_excludes_the_in_progress_day_from_worst_day() -> None:
    """F2: an expensive-so-far but still-accumulating today must never
    win "most expensive period" while it is still in progress. Pass-2:
    same filter reversion as above turns this red -- `worst_day` would
    become the $5.00 in-progress day."""
    periods = [
        _energy_period_dict("2026-01-06", chg_ac_wh=1_000.0, cost=0.20, currency="USD"),
        _energy_period_dict("2026-01-07", chg_ac_wh=5_000.0, cost=1.00, currency="USD"),
        _energy_period_dict("2026-01-08", chg_ac_wh=25_000.0, cost=5.00, currency="USD"),
    ]
    now_ts = int(datetime(2026, 1, 8, 12, 0, tzinfo=UTC).timestamp())  # mid-day on the 8th
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=0.20,
        currency="USD",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=now_ts,
        tz="UTC",
    )
    assert view.worst_day is not None
    assert view.worst_day.period == "2026-01-07"


def test_build_energy_view_model_reports_no_best_or_worst_day_when_every_period_is_in_progress() -> (
    None
):
    """F2: a range that only covers today (no complete period yet)
    must say so honestly -- `best_day`/`worst_day` both `None`, the
    same "unavailable" the template already renders for no tariff --
    rather than crowning the one in-progress period. Pass-2: removing
    the `if complete_rows` guard (falling back to ranking over all
    rows regardless) turns this red."""
    periods = [_energy_period_dict("2026-01-08", chg_ac_wh=50.0, cost=0.01, currency="USD")]
    now_ts = int(datetime(2026, 1, 8, 12, 0, tzinfo=UTC).timestamp())
    view = build_energy_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        granularity="daily",
        periods=periods,
        tariff=0.20,
        currency="USD",
        series_src="/api/v1/energy/daily?device=1",
        now_ts=now_ts,
        tz="UTC",
    )
    assert view.best_day is None
    assert view.worst_day is None


def _grid_row(
    day: str,
    *,
    grid_v_min: float | None = None,
    grid_v_avg: float | None = None,
    grid_v_max: float | None = None,
    grid_hz_min: float | None = None,
    grid_hz_avg: float | None = None,
    grid_hz_max: float | None = None,
    grid_readings: int = 0,
) -> DailyGridRollup:
    return DailyGridRollup(
        device_id=1,
        day=day,
        grid_v_min=grid_v_min,
        grid_v_avg=grid_v_avg,
        grid_v_max=grid_v_max,
        grid_hz_min=grid_hz_min,
        grid_hz_avg=grid_hz_avg,
        grid_hz_max=grid_hz_max,
        grid_readings=grid_readings,
    )


def test_build_grid_view_model_excludes_a_no_data_day_from_the_voltage_average() -> None:
    """grid-quality requirement "Daily Voltage and Frequency Ranges",
    scenario "A day with no grid-present samples reports no range".
    Pass-1: catches a no-data day's `None` fields pulling `typical_v`
    toward zero (e.g. via a naive `sum(...) / len(rows)` over every
    row including the empty one), which would understate a healthy
    grid's real average voltage."""
    rows = [
        _grid_row(
            "2026-01-07", grid_v_min=228.0, grid_v_avg=230.0, grid_v_max=232.0, grid_readings=10
        ),
        _grid_row("2026-01-08"),  # no grid-present reading at all
        _grid_row(
            "2026-01-09", grid_v_min=226.0, grid_v_avg=228.0, grid_v_max=230.0, grid_readings=8
        ),
    ]
    view = build_grid_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        rows=rows,
        threshold_v=50.0,
        series_src="/api/v1/grid/series?device=1",
    )
    assert view.typical_v == (230.0 + 228.0) / 2
    assert view.lowest_v == 226.0
    assert view.highest_v == 232.0
    assert view.days_with_data == 2
    assert view.days_analyzed == 3


def test_build_grid_view_model_reports_frequency_unavailable_separately_from_voltage() -> None:
    """`grid.quality.grid_quality_range`'s own documented asymmetry: a
    present reading can still lack frequency (`judge()` never looks at
    it), so a day can have a real voltage range with no frequency range
    at all. Pass-1: catches treating "no frequency" the same as "no
    voltage" (e.g. by reusing the voltage-present filter for frequency
    too), which would silently fabricate a frequency range from a day
    that reported none."""
    rows = [
        _grid_row(
            "2026-01-07", grid_v_min=228.0, grid_v_avg=230.0, grid_v_max=232.0, grid_readings=10
        ),
        _grid_row(
            "2026-01-08",
            grid_v_min=227.0,
            grid_v_avg=229.0,
            grid_v_max=231.0,
            grid_hz_min=59.8,
            grid_hz_avg=60.0,
            grid_hz_max=60.2,
            grid_readings=12,
        ),
    ]
    view = build_grid_view_model(
        device_records=(_device(1, "SN0001"),),
        selected_device_id=1,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        rows=rows,
        threshold_v=50.0,
        series_src="/api/v1/grid/series?device=1",
    )
    assert view.lowest_hz == 59.8
    assert view.highest_hz == 60.2
    assert view.days_with_data == 2


def test_build_grid_view_model_propagates_threshold_v_and_devices() -> None:
    """Pass-1: catches the page silently showing a hardcoded or default
    threshold instead of the real configured
    `DetectorConfig.threshold_v` the plain-language explanation refers
    to."""
    view = build_grid_view_model(
        device_records=(_device(9, "SN0009"),),
        selected_device_id=9,
        range_start=_RANGE_START,
        range_end=_RANGE_END,
        rows=[],
        threshold_v=42.5,
        series_src="/api/v1/grid/series?device=9",
    )
    assert view.threshold_v == 42.5
    assert view.selected_device_id == 9
    assert view.devices[0].id == 9
    assert view.typical_v is None
    assert view.days_with_data == 0
    assert view.days_analyzed == 0
