"""Integration tests for the overview page (`GET /`): web-ui "One
Device at a Time, With a Selector" and "Empty and Stale States Are
Shown Explicitly" (amendment items 9 and 10).

Built through the real `create_app`/`bootstrap.build` composition root,
the same way `tests/integration/web/test_app.py` already does, so the
page is proven wired end-to-end — not just against a hand-built stub.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.outages.model import Event
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


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


def _build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, devices: str
) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": devices,
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    return bootstrap.build(settings, clock=FakeClock(_NOW))


def test_two_devices_show_one_at_a_time_with_a_named_selector_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "Multiple devices show one at a time with a selector
    offered" + amendment item 10 (the selector is a named element)."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001,TESTDEV0002")
    try:
        first, second = application.device_records
        application.sample_store.add(first.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=81))
        application.sample_store.add(second.id, _NOW_TS - 10, 1, Reading(grid_v=0.0, soc=20))
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert 'name="device"' in html  # amendment item 10: explicit, named selector
        assert html.count("<option") == 2
        assert "81%" in html  # the first configured device's own data is shown
    finally:
        application.database.close()


def test_switching_the_selector_switches_the_shown_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "Switching the selector switches the shown data"."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001,TESTDEV0002")
    try:
        first, second = application.device_records
        application.sample_store.add(first.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=81))
        application.sample_store.add(second.id, _NOW_TS - 10, 1, Reading(grid_v=0.0, soc=20))
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get(f"/?device={second.id}")

        assert "20%" in response.text
        assert "81%" not in response.text
    finally:
        application.database.close()


def test_a_newly_configured_device_with_no_sample_shows_an_explicit_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A device with no data shows an explicit empty state"
    (amendment item 9) — never blank charts or a zero-valued reading."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "0%" not in html
        assert "No data yet" in html
    finally:
        application.database.close()


def test_the_spanish_empty_state_uses_a_neutral_usted_register_not_voseo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """es.json neutral Spanish register normalization: every other
    Spanish string in the catalog addresses the reader as "usted" (for
    example "Vuelva a intentarlo", "Verifique su conexión") -- this
    was the one string still using Argentine voseo ("Volvé a
    revisar"), an inconsistent regional register nowhere else in the
    app. Pass-2: reverting the string back to "Volvé a revisar
    después del próximo sondeo programado." turns this red."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            client.cookies.set("lang", "es")
            response = client.get("/")

        html = response.text
        assert "Vuelva a revisar" in html
        assert "Volvé" not in html
    finally:
        application.database.close()


def test_a_stale_sample_shows_an_explicit_stale_indicator_with_the_data_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "Stale data is marked, not shown as current"."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        (device,) = application.device_records
        application.sample_store.add(device.id, _NOW_TS - 9000, 1, Reading(grid_v=120.0, soc=55))
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "Stale" in html
        # UI-01 (qa-report-ui-01.md): the raw sample age in seconds must
        # never leak into the page; 9000 s floors to a human "2 hours
        # ago" (9000 // 60 = 150 min, 150 // 60 = 2 h).
        assert "9000" not in html
        assert "2 hours ago" in html
    finally:
        application.database.close()


def test_the_page_head_eyebrow_shows_live_status_not_the_heading_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-03 (qa-report-ui-01.md): the eyebrow above the "Overview"
    heading repeated the heading's own text verbatim, adding no
    information. A defect that reverted the eyebrow back to
    `nav.overview` (the same text as the `<h1>`) would resurface the
    same complaint."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )
        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert '<h1 class="page-head__title">Overview</h1>' in html
        assert 'page-head__eyebrow">Live status</p>' in html
    finally:
        application.database.close()


def test_primary_navigation_links_to_outages_and_battery_but_not_unbuilt_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Visual-QA batch fix01, fix 3: `base.html`'s `<nav>` only linked
    to Overview, leaving the already-shipped Outages and Battery pages
    undiscoverable without typing a URL by hand. Energy and Grid have
    no routes yet (slices 29/30), so a defect that linked them too
    would send a visitor to a 404."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        html = response.text
        assert '<a href="/outages">' in html
        assert '<a href="/battery">' in html
        assert '<a href="/energy">' not in html
        assert '<a href="/grid">' not in html
    finally:
        application.database.close()


def test_the_device_choice_persists_via_cookie_across_a_second_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001,TESTDEV0002")
    try:
        first, second = application.device_records
        application.sample_store.add(first.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=81))
        application.sample_store.add(second.id, _NOW_TS - 10, 1, Reading(grid_v=0.0, soc=20))
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            client.get(f"/?device={second.id}")
            response = client.get("/")  # no ?device= this time

        assert "20%" in response.text
        assert "81%" not in response.text
    finally:
        application.database.close()


def test_the_overview_shows_real_power_flows_battery_health_and_outage_tiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-02/UI-04/UI-05 (qa-report-ui-01.md): the overview's bento was
    mostly empty even though every one of these values already exists
    on the latest sample or in the existing outage-derivation pipeline
    `outages_page` already calls. A defect that dropped a tile, wired
    it to the wrong `Reading` field, or left it hardcoded, would either
    show nothing or misreport the device's actual state."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        (device,) = application.device_records
        application.sample_store.add(
            device.id,
            _NOW_TS - 60,
            1,
            Reading(
                soc=80,
                grid_v=120.0,
                solar_in_w=300.0,
                ac_in_w=120.0,
                ac_out_w=150.0,
                batt_in_w=150.0,
                batt_out_w=0.0,
                soh=97.5,
                cycles=42,
                chg_remain_min=90,
            ),
        )
        outage_store = OutageStore(application.database.writer)
        outage_store.replace_from(
            device.id,
            None,
            [Event(start_ts=_NOW_TS - 3600, end_ts=_NOW_TS - 3000, kind="outage")],
            [],
        )
        application.database.writer.commit()
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "300 W" in html  # solar in
        assert "120 W" in html  # grid in
        assert "150 W" in html  # AC load out / net battery power (both 150 W here)
        assert "Charging" in html
        assert "90 min to full" in html
        assert "97.5%" in html  # state of health
        assert ">42<" in html  # cycles
        # C-01 (qa-report-ui-01.md): a raw second count ("600s") used to
        # be shown instead of a human duration -- a 600s (10 min) outage
        # is both the longest outage and the total downtime here.
        assert "10 min" in html
        # C-02 (qa-report-ui-01.md): the "Last update" tile used to
        # render the full "YYYY-MM-DD HH:MM" even for a sample from
        # today's own local day (the sample is `_NOW_TS - 60`, still
        # 2026-01-01 under the default UTC timezone) -- a defect that
        # reverted to that full format would render exactly this string.
        assert "2026-01-01 11:59" not in html
        assert "11:59" in html
    finally:
        application.database.close()


def test_the_overview_never_shows_a_missing_value_as_a_fabricated_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Named Defect "missing read as zero": a device that reports a
    reading at all, but none of the new power-flow/battery-health
    fields on it, must show every one of those tiles as unavailable --
    a defect defaulting an absent field to `0` before display would
    fabricate a measurement the device never reported (e.g. "0 W solar"
    implies "confirmed no sun", not "this device has no solar input
    sensor")."""
    application = _build(monkeypatch, tmp_path, devices="TESTDEV0001")
    try:
        (device,) = application.device_records
        application.sample_store.add(device.id, _NOW_TS - 60, 1, Reading(soc=80, grid_v=120.0))
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app) as client:
            response = client.get("/")

        assert response.status_code == 200
        html = response.text
        assert "0 W" not in html
        cycles_tile = re.search(
            r'<p class="stat-tile__label">Cycles</p>\s*<p class="stat-tile__value">(.*?)</p>',
            html,
        )
        assert cycles_tile is not None
        assert cycles_tile.group(1) == "Unavailable"  # never a fabricated 0
        soh_tile = re.search(
            r'<p class="stat-tile__label">State of health</p>'
            r'\s*<p class="stat-tile__value stat-tile--accent-battery">(.*?)</p>',
            html,
        )
        assert soh_tile is not None
        assert soh_tile.group(1) == "Unavailable"  # never a fabricated 0%
        assert html.count("Unavailable") >= 4  # solar/grid-in/AC-out/battery-power/SoH/cycles
    finally:
        application.database.close()
