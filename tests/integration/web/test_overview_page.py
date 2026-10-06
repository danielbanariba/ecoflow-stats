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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
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
        assert "9000" in html  # the reported data age
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
