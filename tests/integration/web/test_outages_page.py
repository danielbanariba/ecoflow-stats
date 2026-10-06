"""Integration tests for the outages page (`GET /outages`, task 15.5):
the summary, the server-rendered weekday/hour heatmap, the events
table, the gap-review queue count, and the accessibility floor every
later chart-bearing page reuses via `_macros.html`'s
`chart_with_fallback` (task 15.7's REFACTOR target).

Built through the real `create_app`/`bootstrap.build` composition
root, the same harness `test_overview_page.py` already uses, so the
page is proven wired end-to-end -- not just against a hand-built stub.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.outages.model import Decision, Event, Gap
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.imports import ImportRunStore
from ecoflow_stats.storage.legacy import LegacyStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_RANGE_START = _NOW_TS - 7 * 24 * 60 * 60


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _never_ticks(application: bootstrap.Application) -> SupervisedTaskHandle:
    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-collector", target=_blocks_forever, clock=application.clock
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def _build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    return bootstrap.build(settings, clock=FakeClock(_NOW))


def _event(start_ts: int, end_ts: int | None, **overrides: object) -> Event:
    defaults: dict[str, object] = {"kind": "outage"}
    defaults.update(overrides)
    return Event(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _gap(start_ts: int, end_ts: int | None, **overrides: object) -> Gap:
    defaults: dict[str, object] = {
        "cause": "unknown",
        "state_before": "present",
        "state_after": "present",
        "soc_before": 80,
        "soc_after": 75,
        "chg_ac_wh_delta": None,
        "expected_in_wh": None,
        "evidence": "inconclusive",
        "failures": {},
    }
    defaults.update(overrides)
    return Gap(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _decision(device_id: int, target: str, start_ts: int, end_ts: int, verdict: str) -> Decision:
    return Decision(
        device_id=device_id,
        target=target,  # type: ignore[arg-type]
        start_ts=start_ts,
        end_ts=end_ts,
        verdict=verdict,  # type: ignore[arg-type]
        decided_at=1,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    events: list[Event] | None = None,
    gaps: list[Gap] | None = None,
    decisions_factory: Callable[[int], list[Decision]] | None = None,
) -> tuple[bootstrap.Application, int, TestClient]:
    application = _build(monkeypatch, tmp_path)
    (device,) = application.device_records
    outage_store = OutageStore(application.database.writer)
    outage_store.replace_from(device.id, None, events or [], gaps or [])
    if decisions_factory is not None:
        decision_store = DecisionStore(application.database.writer)
        for decision in decisions_factory(device.id):
            decision_store.add(decision)
    application.database.writer.commit()
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    return application, device.id, TestClient(app)


def _dd_value(html: str, label: str) -> str:
    pattern = re.compile(rf"<dt>{re.escape(label)}</dt>\s*<dd>(.*?)</dd>", re.DOTALL)
    match = pattern.search(html)
    assert match, f"no <dd> found for label {label!r} in:\n{html}"
    return match.group(1).strip()


def test_the_page_renders_the_outage_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 1: "the page renders the summary (count, total, longest,
    mean, brief count, unknown time)". Pass-1: a summary that drops a
    field, or computes mean/total/longest from the wrong source, would
    silently under-report the page's headline numbers."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        events=[
            _event(_RANGE_START + 1_000, _RANGE_START + 1_000 + 4_321),
            _event(_RANGE_START + 50_000, _RANGE_START + 50_000 + 1_111),
            _event(_RANGE_START + 90_000, _RANGE_START + 90_030, kind="brief"),
        ],
        gaps=[_gap(_RANGE_START + 10_000, _RANGE_START + 10_222)],
    )
    try:
        with client:
            response = client.get("/outages")

        assert response.status_code == 200
        html = response.text
        assert _dd_value(html, "Outage count") == "2"
        assert _dd_value(html, "Total downtime") == "5432s"
        assert _dd_value(html, "Longest outage") == "4321s"
        assert _dd_value(html, "Mean duration") == "2716s"
        assert _dd_value(html, "Brief drops") == "1"
        assert _dd_value(html, "Unknown time") == "222s"
    finally:
        application.database.close()


def test_the_summary_reports_unavailable_mean_and_longest_with_no_outages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: with zero outages, a naive implementation would either
    crash (division by zero) or dishonestly show "0s" instead of
    reporting the value as unavailable."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        assert _dd_value(html, "Outage count") == "0"
        assert _dd_value(html, "Longest outage") == "Unavailable"
        assert _dd_value(html, "Mean duration") == "Unavailable"
    finally:
        application.database.close()


def test_the_heatmap_is_rendered_server_side_from_the_outage_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 2: the weekday x hour heatmap is consumed server-side,
    not client-fetched (task instruction, a deliberate deviation from
    the design's default data-src pattern). Pass-1: a template that
    client-fetches this instead, or embeds the wrong aggregate fields,
    would leave an empty or wrong `heatmap-data` JSON block."""
    outage_dt = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)  # a Monday, 03:00 UTC
    outage_ts = int(outage_dt.timestamp())
    application, _device_id, client = _client(
        monkeypatch, tmp_path, events=[_event(outage_ts, outage_ts + 60)]
    )
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        match = re.search(
            r'<script type="application/json" id="heatmap-data">(.*?)</script>', html, re.DOTALL
        )
        assert match, "expected a server-rendered heatmap-data JSON block"
        payload = json.loads(match.group(1))
        assert payload["hour_of_day"][outage_dt.hour] == 1
        assert sum(payload["hour_of_day"]) == 1
        assert payload["day_of_week"][outage_dt.weekday()] == 1
        assert sum(payload["day_of_week"]) == 1
        # The same numbers must also appear in the server-rendered
        # fallback table (accessibility floor), not only the JS payload.
        assert re.search(r"<td>3</td>\s*<td>1</td>", html)
    finally:
        application.database.close()


def test_the_events_table_shows_start_end_uncertainty_and_a_detected_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 3: "the events table with each event's start/end
    uncertainty and source". A legacy-sourced row's own rendering is
    covered by `test_a_real_imported_legacy_outage_appears_on_the_page`
    below; this one exercises "detected"."""
    outage_ts = _RANGE_START + 2_000
    application, _device_id, client = _client(
        monkeypatch, tmp_path, events=[_event(outage_ts, outage_ts + 90, start_in_gap=True)]
    )
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        assert "Detected" in html
        assert "Start uncertain" in html
        assert "End uncertain" not in html
    finally:
        application.database.close()


def test_a_real_imported_legacy_outage_appears_on_the_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DATA-02 (qa-report-data-01.md): an outage imported from the
    legacy `outages.log` used to never reach this page at all --
    `outages_page` hardcoded `legacy=()` because `storage.legacy.
    LegacyStore` had no range query (tracked gap, apply-progress-
    batch13). Proves the full chain end-to-end: a legacy entry seeded
    directly in storage (mirroring what `history_import.service.
    run_import` actually writes) now shows up in both the summary
    count and the events table as a "Legacy" source."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        import_id = ImportRunStore(application.database.writer).start(device_id, 1, "UTC")
        legacy_start = _RANGE_START + 4_000
        LegacyStore(application.database.writer).upsert(
            device_id=device_id,
            start_ts=legacy_start,
            end_ts=legacy_start + 300,
            soc_start=70,
            soc_end=60,
            logged_minutes=5,
            start_line="corte",
            end_line="retorno",
            source_tz="UTC",
            flags=frozenset(),
            import_id=import_id,
        )
        application.database.writer.commit()

        with client:
            response = client.get("/outages")

        html = response.text
        assert _dd_value(html, "Outage count") == "1"
        assert "Legacy" in html
    finally:
        application.database.close()


def test_the_gap_review_queue_count_excludes_an_already_decided_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 4: "the gap-review queue count". Pass-1: a route that
    counted every gap regardless of decision would re-surface an
    already-reviewed gap in the queue forever."""
    unresolved_start = _RANGE_START + 3_000
    resolved_start = _RANGE_START + 6_000
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        gaps=[
            _gap(unresolved_start, unresolved_start + 300),
            _gap(resolved_start, resolved_start + 300),
        ],
        decisions_factory=lambda device_id: [
            _decision(device_id, "gap", resolved_start, resolved_start + 300, "outage")
        ],
    )
    try:
        with client:
            response = client.get("/outages")

        assert "1 gap awaiting review" in response.text
        assert "1 gaps awaiting review" not in response.text
    finally:
        application.database.close()


def test_the_legacy_review_section_renders_with_its_expand_button_and_pending_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-11 (qa-report-ui-01.md): the legacy-review decide/undo routes
    already existed with no UI entry point reaching them at all. Pass-1:
    a page that never rendered this section (or never counted a
    pending suspected-phantom entry) would leave the feature wholly
    unreachable, exactly the defect this finding reports -- a confirmed
    (non-pending) legacy entry must NOT inflate the count, mirroring
    `test_the_gap_review_queue_count_excludes_an_already_decided_gap`."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    import_id = ImportRunStore(application.database.writer).start(device_id, 1, "UTC")
    pending_start = _RANGE_START + 2_000
    confirmed_start = _RANGE_START + 7_000
    legacy_store = LegacyStore(application.database.writer)
    legacy_store.upsert(
        device_id=device_id,
        start_ts=pending_start,
        end_ts=pending_start + 120,
        soc_start=0,
        soc_end=0,
        logged_minutes=2,
        start_line="corte",
        end_line="retorno",
        source_tz="UTC",
        flags=frozenset({"suspected_phantom"}),
        import_id=import_id,
    )
    legacy_store.upsert(
        device_id=device_id,
        start_ts=confirmed_start,
        end_ts=confirmed_start + 120,
        soc_start=0,
        soc_end=0,
        logged_minutes=2,
        start_line="corte",
        end_line="retorno",
        source_tz="UTC",
        flags=frozenset({"suspected_phantom"}),
        import_id=import_id,
    )
    DecisionStore(application.database.writer).add(
        _decision(device_id, "legacy", confirmed_start, confirmed_start + 120, "real")
    )
    application.database.writer.commit()
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        assert "1 legacy outage awaiting review" in html
        assert "1 legacy outages awaiting review" not in html
        assert 'hx-get="/outages/legacy?device=' in html
        assert "Review legacy outages" in html
    finally:
        application.database.close()


def test_every_chart_element_has_an_aria_label_and_a_details_fallback_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario 5, the accessibility floor: "every [data-chart] element
    has a server-rendered <details> fallback table and an aria-label
    summary". Pass-1: a chart missing either would be meaningless to a
    screen-reader user -- this is the floor every later chart-bearing
    page (battery, energy, grid) inherits via `chart_with_fallback`."""
    application, _device_id, client = _client(
        monkeypatch, tmp_path, events=[_event(_RANGE_START + 1_000, _RANGE_START + 1_600)]
    )
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        chart_divs = re.findall(r'<div[^>]*data-chart="[^"]+"[^>]*>', html)
        assert len(chart_divs) == 2, "expected exactly the heatmap and mains-strip charts"
        for div in chart_divs:
            assert 'aria-label="' in div and 'aria-label=""' not in div

        fallback_blocks = re.findall(
            r'<details class="chart-fallback">.*?</details>', html, re.DOTALL
        )
        assert len(fallback_blocks) == 2
        for block in fallback_blocks:
            assert "<table>" in block
    finally:
        application.database.close()


def test_the_scan_actually_detects_a_chart_missing_its_fallback() -> None:
    """Pass-2, proven directly: a chart figure with no `<details>`
    fallback must fail the same check the previous test runs, so that
    test is not vacuously true against a page with no charts at all."""
    offending_html = '<div data-chart="heatmap" aria-label="x"></div>'

    fallback_blocks = re.findall(r'<details class="chart-fallback">.*?</details>', offending_html)
    chart_divs = re.findall(r'<div[^>]*data-chart="[^"]+"[^>]*>', offending_html)

    assert len(chart_divs) == 1
    assert fallback_blocks == []


def test_the_page_has_no_inline_executable_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: CSP (`script-src 'self'`, no `unsafe-inline`) means a
    browser silently blocks any inline `<script>` without a `src` --
    this would surface as a chart that never renders, with no visible
    error. A `type="application/json"` data block is not executable and
    stays allowed (task constraint: "no inline scripts... all chart-init
    code lives in the external app.js")."""
    application, _device_id, client = _client(
        monkeypatch, tmp_path, events=[_event(_RANGE_START + 1_000, _RANGE_START + 1_600)]
    )
    try:
        with client:
            response = client.get("/outages")

        html = response.text
        offenders = [
            tag
            for tag in re.findall(r"<script[^>]*>", html)
            if 'src="' not in tag and 'type="application/json"' not in tag
        ]
        assert offenders == []
    finally:
        application.database.close()


def test_the_page_head_eyebrow_shows_the_selected_range_not_the_heading_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-03 (qa-report-ui-01.md): the eyebrow above the "Outages"
    heading repeated the heading's own text verbatim, adding no
    information. A defect that reverted the eyebrow back to
    `outages.page.title` (the same text as the `<h1>`), or dropped the
    selected range from it, would resurface the same complaint."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/outages")

        assert response.status_code == 200
        html = response.text
        assert '<h1 class="page-head__title">Outages</h1>' in html
        assert 'page-head__eyebrow">Outages</p>' not in html
        assert "2026-01-03" in html
        assert "2026-01-10" in html
    finally:
        application.database.close()


def test_the_gap_review_intro_explains_what_a_gap_is_not_just_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Design critique (qa-report-ui-01.md): the gap-review section
    showed only a bare "N gaps awaiting review" count, with no
    explanation that a gap is unconfirmed missing data -- not a
    confirmed outage. A reader could mistake the count for confirmed
    outages. Pass-2: removing the new intro paragraph (keeping only
    the pre-existing count sentence) turns this red."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            html = client.get("/outages").text

        assert "A gap is a stretch with no data at all" in html
        assert "not a confirmed outage" in html
    finally:
        application.database.close()


def test_the_mains_strip_caption_is_distinct_from_its_own_aria_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Design critique (qa-report-ui-01.md): the mains-strip chart's
    visible caption just repeated its own aria-label text verbatim,
    telling a sighted user nothing a screen-reader summary did not
    already say. Pass-1: the visible caption must now explain what the
    solid-vs-hatched segments actually mean. Pass-2: reverting the
    caption's i18n key back to `outages.mains_strip.aria_label` turns
    this red -- the caption would show the aria-label's own sentence
    instead of the new explanatory one, and this assertion would find
    neither "hatched" nor "recorded" anywhere in a caption paragraph."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            html = client.get("/outages").text

        assert "a hatched segment means no data was recorded" in html
        assert (
            'aria-label="Timeline of grid presence, outages, and unknown time over the'
            ' selected range"' in html
        )
    finally:
        application.database.close()
