"""Integration tests for the battery page (`GET /battery`, task 17.9)
and its 3 read-only API routes (`battery/series`, `battery/trends`,
`battery/outages`, task 17.10): the charge line, the depth-of-discharge
table per outage, the cycle/state-of-health trend, and the observed-
autonomy table -- reusing the accessibility floor `_macros.html`'s
`chart_with_fallback` already established on the outages page (task
15.7's REFACTOR target), never duplicating that markup pattern.

Built through the real `create_app`/`bootstrap.build` composition
root, the same harness `test_outages_page.py` already uses, so both
the page and its own API routes are proven wired end-to-end against a
real `Application` (and therefore a real `app.state.application`, the
cross-context read `web.routes.api`/`web.routes.pages` use to reach a
`RollupStore` without a new `ApiContext`/`PagesContext` field).
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.outages.model import Event
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW_TS = 1_767_916_800  # 2026-01-09T00:00:00Z
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
    from datetime import UTC, datetime

    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    now = datetime.fromtimestamp(_NOW_TS, tz=UTC)
    return bootstrap.build(settings, clock=FakeClock(now))


def _event(start_ts: int, end_ts: int | None, **overrides: object) -> Event:
    defaults: dict[str, object] = {"kind": "outage"}
    defaults.update(overrides)
    return Event(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    events: list[Event] | None = None,
    samples: list[tuple[int, Reading]] | None = None,
    rollups: list[dict[str, object]] | None = None,
) -> tuple[bootstrap.Application, int, TestClient]:
    application = _build(monkeypatch, tmp_path)
    (device,) = application.device_records
    outage_store = OutageStore(application.database.writer)
    outage_store.replace_from(device.id, None, events or [], [])
    sample_store = SampleStore(application.database.writer)
    for ts, reading in samples or []:
        sample_store.add(device.id, ts, 1, reading)
    rollup_store = RollupStore(application.database.writer)
    for row in rollups or []:
        rollup_store.upsert(device.id, **row)  # type: ignore[arg-type]
    application.database.writer.commit()
    app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)
    return application, device.id, TestClient(app)


def _rollup_row(
    day: str,
    *,
    soc_min: int | None = None,
    soc_max: int | None = None,
    cycles_last: int | None = None,
    soh_last: float | None = None,
    batt_temp_max: float | None = None,
) -> dict[str, object]:
    return {
        "day": day,
        "soc_min": soc_min,
        "soc_max": soc_max,
        "cycles_last": cycles_last,
        "soh_last": soh_last,
        "batt_temp_max": batt_temp_max,
    }


# --- page tests -------------------------------------------------------


def test_the_charge_line_chart_renders_with_its_fallback_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario: "the page renders the charge line". Pass-1: a defect
    that fetched the wrong range, or dropped the fallback table, would
    leave a screen-reader user with no way to know the device's charge
    history at all."""
    samples = [
        (_RANGE_START + 100, Reading(soc=95)),
        (_RANGE_START + 200, Reading(soc=90)),
        (_RANGE_START + 300, Reading(soc=85)),
    ]
    application, _device_id, client = _client(monkeypatch, tmp_path, samples=samples)
    try:
        with client:
            response = client.get("/battery")

        assert response.status_code == 200
        html = response.text
        assert 'data-chart="soc-line"' in html
        rows = re.findall(r"<td>[^<]*</td>\s*<td>(\d+)%</td>", html)
        assert rows == ["95", "90", "85"]
    finally:
        application.database.close()


def test_the_dod_table_shows_a_computed_value_and_unavailable_for_a_missing_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "Missing boundary charge is reported as unavailable,
    not assumed" (amendment item 6), surfaced on the page. Pass-1: a
    defect that fabricated a `0`, or dropped the row entirely, would
    misreport an outage's real discharge depth."""
    known_start = _RANGE_START + 1_000
    missing_start = _RANGE_START + 50_000
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        events=[
            _event(known_start, known_start + 7_200, soc_start=90, soc_min=60),
            _event(missing_start, missing_start + 7_200, soc_start=None, soc_min=60),
        ],
    )
    try:
        with client:
            response = client.get("/battery")

        html = response.text
        assert "30%" in html
        assert "Unavailable" in html
    finally:
        application.database.close()


def test_the_autonomy_table_shows_the_comparison_unavailable_and_not_enough_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenarios "Observed duration is shown alongside the device's
    estimate" and "A missing estimate is reported as unavailable, not
    fabricated" (amendment item 7), plus the too-short "not enough
    data" case -- all three distinct rows on one page. Pass-1: a
    defect that conflated "unavailable" with "not enough data", or
    dropped either guard, would misrepresent which outages actually
    have a usable comparison."""
    qualifying_start = _RANGE_START + 1_000
    missing_estimate_start = _RANGE_START + 50_000
    too_short_start = _RANGE_START + 90_000
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        events=[
            _event(
                qualifying_start,
                qualifying_start + 7_200,
                soc_start=90,
                soc_min=60,
                dsg_remain_min_start=240,
            ),
            _event(
                missing_estimate_start,
                missing_estimate_start + 7_200,
                soc_start=90,
                soc_min=60,
                dsg_remain_min_start=None,
            ),
            _event(too_short_start, too_short_start + 600, soc_start=90, soc_min=60),
        ],
    )
    try:
        with client:
            response = client.get("/battery")

        html = response.text
        assert "6.0h" in html
        assert "4.0h" in html
        assert "Not enough data" in html
        assert "Unavailable" in html
    finally:
        application.database.close()


def test_the_trend_table_renders_rollup_rows_in_day_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "Trend reflects the device's own reported progression",
    surfaced on the page. Pass-1: a defect that rendered rollup days
    out of order, or dropped a field, would misdraw the cycle/SoH
    trend table even though every underlying value was itself
    correct."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[
            _rollup_row("2026-01-05", cycles_last=11, soh_last=97.0),
            _rollup_row("2026-01-04", cycles_last=10, soh_last=98.0),
        ],
    )
    try:
        with client:
            response = client.get("/battery")

        html = response.text
        assert 'data-chart="battery-trend"' in html
        first = html.index("2026-01-04")
        second = html.index("2026-01-05")
        assert first < second
        assert "10" in html
        assert "98.0%" in html
    finally:
        application.database.close()


def test_every_chart_element_has_an_aria_label_and_a_details_fallback_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The accessibility floor every chart-bearing page inherits via
    `chart_with_fallback` (task 15.7's REFACTOR target, task 17.9's own
    reuse instruction). Pass-1: a chart missing either would be
    meaningless to a screen-reader user."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/battery")

        html = response.text
        chart_divs = re.findall(r'<div[^>]*data-chart="[^"]+"[^>]*>', html)
        assert len(chart_divs) == 2, "expected exactly the soc-line and battery-trend charts"
        for div in chart_divs:
            assert 'aria-label="' in div and 'aria-label=""' not in div

        fallback_blocks = re.findall(
            r'<details class="chart-fallback">.*?</details>', html, re.DOTALL
        )
        assert len(fallback_blocks) == 2
    finally:
        application.database.close()


def test_the_page_has_no_inline_executable_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: CSP (`script-src 'self'`) silently blocks any inline
    `<script>` without a `src` -- this would surface as a chart that
    never renders, with no visible error (same guard
    `test_outages_page.py` already proves for its own page)."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/battery")

        html = response.text
        offenders = [
            tag
            for tag in re.findall(r"<script[^>]*>", html)
            if 'src="' not in tag and 'type="application/json"' not in tag
        ]
        assert offenders == []
    finally:
        application.database.close()


# --- API route tests ---------------------------------------------------


def test_battery_series_route_returns_the_charge_history_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a route that forgot to sort, or fetched the wrong range,
    would silently feed the wrong data to the charge-line chart."""
    samples = [
        (_RANGE_START + 300, Reading(soc=85)),
        (_RANGE_START + 100, Reading(soc=95)),
    ]
    application, device_id, client = _client(monkeypatch, tmp_path, samples=samples)
    try:
        with client:
            response = client.get(
                "/api/v1/battery/series",
                params={"device": device_id, "from": _RANGE_START, "to": _NOW_TS},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["points"] == [
            {"ts": _RANGE_START + 100, "soc": 95},
            {"ts": _RANGE_START + 300, "soc": 85},
        ]
    finally:
        application.database.close()


def test_battery_trends_route_returns_rollup_rows_in_day_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a route that queried the wrong day range, or returned
    rows unordered, would silently break the cycle/SoH trend chart's
    only data source."""
    application, device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[
            _rollup_row("2026-01-05", cycles_last=11, soh_last=97.0),
            _rollup_row("2026-01-04", cycles_last=10, soh_last=98.0),
        ],
    )
    try:
        with client:
            response = client.get(
                "/api/v1/battery/trends",
                params={"device": device_id, "from": _RANGE_START, "to": _NOW_TS},
            )

        assert response.status_code == 200
        body = response.json()
        assert [day["day"] for day in body["days"]] == ["2026-01-04", "2026-01-05"]
        assert body["days"][0]["cycles_last"] == 10
        assert body["days"][1]["soh_last"] == 97.0
    finally:
        application.database.close()


def test_battery_outages_route_reports_dod_and_autonomy_null_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a route that fabricated a DoD or autonomy value on
    missing data (rather than reporting "unavailable"/"not enough
    data") would feed the battery page's tables a lie neither amendment
    6 nor 7 allows. Also proves a brief event is excluded (never
    carries the telemetry either function needs)."""
    outage_start = _RANGE_START + 1_000
    brief_start = _RANGE_START + 50_000
    application, device_id, client = _client(
        monkeypatch,
        tmp_path,
        events=[
            _event(
                outage_start,
                outage_start + 7_200,
                soc_start=None,
                soc_min=60,
                dsg_remain_min_start=240,
            ),
            _event(brief_start, brief_start + 60, kind="brief", soc_start=90, soc_min=89),
        ],
    )
    try:
        with client:
            response = client.get(
                "/api/v1/battery/outages",
                params={"device": device_id, "from": _RANGE_START, "to": _NOW_TS},
            )

        assert response.status_code == 200
        body = response.json()
        assert len(body["outages"]) == 1
        row = body["outages"][0]
        assert row["start_ts"] == outage_start
        assert row["depth_of_discharge"] == "unavailable"
        assert row["observed_autonomy"] == "not enough data"
    finally:
        application.database.close()


def test_every_new_battery_route_is_get_only_with_no_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a stray mutating method on a battery route would violate
    the read-only API contract every other `/api/v1` route already
    holds (`test_api_outages.py`'s own equivalent check)."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        paths = ("/api/v1/battery/series", "/api/v1/battery/trends", "/api/v1/battery/outages")
        query = {"device": device_id, "from": _RANGE_START, "to": _NOW_TS}

        with client:
            for path in paths:
                post_response = client.post(path, params=query)
                assert post_response.status_code == 405
    finally:
        application.database.close()
