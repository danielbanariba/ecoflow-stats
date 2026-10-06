"""Integration tests for redesign01's overview hero: the battery ring's
real server-rendered percent (not a JS-only placeholder), the Spanish
catalog actually reaching the new strings, and the hero honestly
reflecting an outage rather than always showing "grid present".

Built through the real `create_app`/`bootstrap.build` composition root,
the same way the other `tests/integration/web/*.py` modules do.
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
from ecoflow_stats.web.security import CSRF_COOKIE, csrf_token
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


def _client(application: bootstrap.Application) -> TestClient:
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    return TestClient(app)


def test_overview_renders_the_real_battery_percent_inside_the_ring_without_js(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The activity ring must be server-rendered with the device's real
    charge, not built from scratch by JavaScript (the mockup's own
    `ring.js` left the ring host empty without JS -- a regression this
    app cannot repeat, since `TestClient` never executes JS and a
    screen reader / no-JS visitor never will either). Pass-1: a ring
    that depends on JS to show any number at all would render an empty
    circle with no battery percentage for every one of those visitors."""
    application = _build(monkeypatch, tmp_path)
    try:
        (device,) = application.device_records
        application.sample_store.add(device.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=73))
        with _client(application) as client:
            html = client.get("/").text

        assert 'class="ring-center__value num">73%</span>' in html
        # The ring's progress arc is set to its real final offset directly
        # by the server -- never left at a full/empty circle for JS to fix.
        assert 'data-ring-offset="' in html
    finally:
        application.database.close()


def test_the_spanish_catalog_reaches_the_new_overview_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`test_translation_parity.py` only proves `en.json`/`es.json` have
    the same keys -- it never proves a real page actually requests the
    Spanish value for a *new* redesign01 key at render time. Pass-1: a
    template that hardcoded the English literal for a brand-new string
    (instead of calling `t(...)`) would pass the parity test while
    still showing English text to a Spanish-speaking visitor."""
    application = _build(monkeypatch, tmp_path)
    try:
        (device,) = application.device_records
        application.sample_store.add(device.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=50))
        with _client(application) as client:
            client.get("/")  # mints the real efs_csrf cookie
            headers = {"x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])}
            preferences_response = client.post(
                "/preferences", data={"lang": "es"}, headers=headers, follow_redirects=False
            )
            assert preferences_response.status_code == 303  # accepted, not CSRF-rejected
            html = client.get("/").text

        assert "Red" in html  # live.grid_label (es)
        assert "carga" in html  # live.ring_label (es)
    finally:
        application.database.close()


def test_an_absent_grid_renders_the_outage_hero_not_the_present_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hero must honestly reflect the real grid state (scope rule
    "Map the hero to the real states ... honestly"). Pass-1: a hero
    that hardcoded the "present" styling and copy -- or that only
    checked `view.grid` for one branch -- would show "grid power is
    present" during an actual outage, the single worst moment for this
    page to lie."""
    application = _build(monkeypatch, tmp_path)
    try:
        (device,) = application.device_records
        application.sample_store.add(device.id, _NOW_TS - 10, 1, Reading(grid_v=0.0, soc=40))
        with _client(application) as client:
            html = client.get("/").text

        assert "Grid power is out" in html
        assert "Grid power is present" not in html
        assert "hero-tile__headline--absent" in html
        assert "hero-tile__headline--present" not in html
    finally:
        application.database.close()
