"""Integration tests for the grid page (`GET /grid`, task 20.3/20.4's
page half): daily voltage/frequency ranges, summary cards (typical
voltage, lowest/highest, frequency stability), and a plain-language
explanation of what's healthy for the configured outage threshold.

Built through the real `create_app`/`bootstrap.build` composition root
(the same harness `test_battery_page.py`/`test_energy_page.py` already
use), so the page is proven wired end to end against a real
`Application` -- including the real `ApiContext.detector_config.
threshold_v` wiring `web.app._build_api_context` only produces from
real settings.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW_TS = 1_767_916_800  # 2026-01-09T00:00:00Z


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _never_ticks(application: bootstrap.Application) -> SupervisedTaskHandle:
    import asyncio

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(name="test-job", target=_blocks_forever, clock=application.clock)
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def _build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, threshold_v: float | None = None
) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    if threshold_v is not None:
        env["ECOFLOW_STATS_OUTAGE_THRESHOLD_V"] = str(threshold_v)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    now = datetime.fromtimestamp(_NOW_TS, tz=UTC)
    return bootstrap.build(settings, clock=FakeClock(now))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    threshold_v: float | None = None,
    rollups: list[dict[str, object]] | None = None,
) -> tuple[bootstrap.Application, int, TestClient]:
    application = _build(monkeypatch, tmp_path, threshold_v=threshold_v)
    (device,) = application.device_records
    rollup_store = RollupStore(application.database.writer)
    for row in rollups or []:
        rollup_store.upsert_grid(device.id, **row)  # type: ignore[arg-type]
    application.database.writer.commit()
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    client = TestClient(app)
    return application, device.id, client


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
) -> dict[str, object]:
    return {
        "day": day,
        "grid_v_min": grid_v_min,
        "grid_v_avg": grid_v_avg,
        "grid_v_max": grid_v_max,
        "grid_hz_min": grid_hz_min,
        "grid_hz_avg": grid_hz_avg,
        "grid_hz_max": grid_hz_max,
        "grid_readings": grid_readings,
    }


def test_the_grid_page_shows_voltage_stat_cards_from_rollup_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """grid-quality requirement "Daily Voltage and Frequency Ranges".
    Pass-1: catches the page route failing to read the persisted grid
    rollup rows at all, or showing a fabricated/placeholder voltage
    instead of the real stored range."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[
            _grid_row(
                "2026-01-08",
                grid_v_min=228.0,
                grid_v_avg=230.0,
                grid_v_max=232.0,
                grid_hz_min=59.8,
                grid_hz_avg=60.0,
                grid_hz_max=60.2,
                grid_readings=120,
            )
        ],
    )
    try:
        with client:
            response = client.get("/grid")
        assert response.status_code == 200
        html = response.text
        assert "230" in html
        assert "228" in html
        assert "232" in html
    finally:
        application.database.close()


def test_the_grid_page_explains_the_configured_threshold_in_plain_language(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """grid-quality's own configurable `outage_threshold_v`
    (`DetectorConfig.threshold_v`). Pass-1: catches the page showing a
    hardcoded default threshold (e.g. always "50") instead of the real
    configured value, which would mislead a reviewer who tuned it."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        threshold_v=45.0,
        rollups=[
            _grid_row(
                "2026-01-08", grid_v_min=228.0, grid_v_avg=230.0, grid_v_max=232.0, grid_readings=10
            )
        ],
    )
    try:
        with client:
            response = client.get("/grid")
        assert response.status_code == 200
        assert "45" in response.text
    finally:
        application.database.close()


def test_the_grid_page_shows_a_no_data_day_as_unavailable_not_a_fabricated_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """grid-quality scenario "A day with no grid-present samples
    reports no range". Pass-1: catches the daily table rendering a
    no-data day's `None` fields as a fabricated "0.0 V" instead of the
    honest unavailable label."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[_grid_row("2026-01-08")],  # no grid-present reading at all
    )
    try:
        with client:
            response = client.get("/grid")
        assert response.status_code == 200
        html = response.text
        assert "Unavailable" in html
        # Not ">0 V<" or ">0.0 V<" -- a bare fabricated zero immediately
        # inside its own table cell. Not a plain substring check: the
        # default 50 V outage threshold's own explainer text legitimately
        # ends in "50 V", which contains the substring "0 V" without being
        # a fabricated reading.
        assert ">0.0 V<" not in html
        assert ">0 V<" not in html
    finally:
        application.database.close()


def test_unknown_device_on_the_grid_page_returns_a_404_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API-02's page-half convention (qa-fix-d), reused by every page
    through `_resolve_device`. Pass-1: catches a new route reading the
    `device` query param directly instead of through that shared
    guard, which would silently 200 on a stale or typo'd device id."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/grid", params={"device": 9999})
        assert response.status_code == 404
    finally:
        application.database.close()
