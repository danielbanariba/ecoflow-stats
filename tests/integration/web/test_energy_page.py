"""Integration tests for the energy page (`GET /energy`, task 19.1/19.2's
page half): daily (or monthly beyond 90 days) kWh in/out by source, the
configured tariff's cost, and each period's estimate flags.

Built through the real `create_app`/`bootstrap.build` composition root
(the same harness `test_battery_page.py` already uses), so the page is
proven wired end to end against a real `Application` -- including the
real `ApiContext.tariff`/`currency` wiring `web.app._build_api_context`
only produces from real settings, which the lightweight standalone-
`FastAPI()` harness `test_api_energy.py` uses cannot exercise.
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tariff: float | None = None,
    currency: str = "",
) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    if tariff is not None:
        env["ECOFLOW_STATS_TARIFF"] = str(tariff)
    if currency:
        env["ECOFLOW_STATS_CURRENCY"] = currency
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    now = datetime.fromtimestamp(_NOW_TS, tz=UTC)
    return bootstrap.build(settings, clock=FakeClock(now))


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tariff: float | None = None,
    currency: str = "",
    rollups: list[dict[str, object]] | None = None,
) -> tuple[bootstrap.Application, int, TestClient]:
    application = _build(monkeypatch, tmp_path, tariff=tariff, currency=currency)
    (device,) = application.device_records
    rollup_store = RollupStore(application.database.writer)
    for row in rollups or []:
        rollup_store.upsert_energy(device.id, **row)  # type: ignore[arg-type]
    application.database.writer.commit()
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    client = TestClient(app)
    return application, device.id, client


def _energy_row(day: str, *, chg_ac_wh: float = 0.0, energy_flags: int = 0) -> dict[str, object]:
    return {
        "day": day,
        "chg_ac_wh": chg_ac_wh,
        "chg_dc_wh": 0.0,
        "chg_solar_wh": 0.0,
        "dsg_ac_wh": 0.0,
        "dsg_dc_wh": 0.0,
        "chg_ac_est_wh": 0.0,
        "energy_flags": energy_flags,
    }


def test_the_energy_page_shows_the_total_cost_from_a_configured_tariff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Energy requirement "Energy Cost from a Configurable Flat Tariff".
    Pass-1: catches the page route failing to read the persisted energy
    rollup rows at all, or computing the wrong cost (missing the Wh-to-
    kWh conversion, or using the wrong counter)."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        tariff=0.20,
        currency="USD",
        rollups=[_energy_row("2026-01-08", chg_ac_wh=5_000.0)],  # 5 kWh
    )
    try:
        with client:
            response = client.get("/energy")
        assert response.status_code == 200
        html = response.text
        assert "1.00" in html  # 5 kWh * $0.20/kWh
        assert "USD" in html
    finally:
        application.database.close()


def test_the_energy_page_shows_a_set_a_tariff_note_without_a_configured_tariff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "No configured tariff shows energy without a fabricated
    cost". Pass-1: catches cost silently rendering as a fabricated "0"
    instead of the honest note the spec requires."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        tariff=None,
        rollups=[_energy_row("2026-01-08", chg_ac_wh=5_000.0)],
    )
    try:
        with client:
            response = client.get("/energy")
        assert response.status_code == 200
        html = response.text
        assert "Set a tariff" in html
        assert "$0" not in html
        assert ">0<" not in html
    finally:
        application.database.close()


def test_the_energy_page_switches_to_monthly_periods_beyond_90_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Task 19.1's granularity rule, proven through the page, not just
    the API. Pass-1: catches the page always rendering a daily table
    regardless of range length, disagreeing with the chart it sits
    beside (which picks monthly for the same range)."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[
            _energy_row("2025-10-02", chg_ac_wh=100.0),
            _energy_row("2026-01-05", chg_ac_wh=50.0),
        ],
    )
    try:
        with client:
            response = client.get("/energy", params={"from": _NOW_TS - 100 * 86_400, "to": _NOW_TS})
        assert response.status_code == 200
        html = response.text
        assert "2025-10" in html
        assert "2026-01" in html
        assert "2025-10-02" not in html
    finally:
        application.database.close()


def test_the_energy_table_shows_an_estimate_flag_per_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The energy page's own scope: "a table with estimate flags
    ... visible per row". Pass-1: catches the page decoding no flags at
    all, silently hiding a counter-reset/implausible-jump/gap-prorated
    day from the reviewer who needs to know the number is an estimate."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        rollups=[_energy_row("2026-01-08", chg_ac_wh=100.0, energy_flags=1)],  # counter_reset
    )
    try:
        with client:
            response = client.get("/energy")
        assert response.status_code == 200
        html = response.text
        assert "Counter reset" in html
    finally:
        application.database.close()


def test_the_energy_page_never_crowns_the_in_progress_day_as_cheapest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F2 (orchestrator QA batch F): `_NOW_TS` is exactly midnight UTC
    on 2026-01-09, so that day's own rollup row (a few seconds of
    real data at most) is still in progress. Its artificially tiny
    cost must never win "Cheapest period" over 2026-01-08, the
    genuinely complete, truly cheapest day. Pass-1: catches the route
    failing to wire `now`/`tz` through to `build_energy_view_model`,
    so an in-progress day wins `best_day` again -- the exact
    "zero production callers" gap class this project already guards
    against for its pure builders."""
    application, _device_id, client = _client(
        monkeypatch,
        tmp_path,
        tariff=0.20,
        currency="USD",
        rollups=[
            _energy_row("2026-01-07", chg_ac_wh=5_000.0),  # $1.00, complete
            _energy_row("2026-01-08", chg_ac_wh=100.0),  # $0.02, complete, true cheapest
            _energy_row("2026-01-09", chg_ac_wh=5.0),  # $0.001, today, still in progress
        ],
    )
    try:
        with client:
            response = client.get("/energy")
        assert response.status_code == 200
        html = response.text
        best_day_card = html[html.index("Cheapest period") : html.index("Most expensive period")]
        assert "2026-01-09" not in best_day_card
        assert "Jan 8" in best_day_card
    finally:
        application.database.close()


def test_unknown_device_on_the_energy_page_returns_a_404_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API-02's page-half convention (qa-fix-d), reused by every page
    through `_resolve_device`. Pass-1: catches a new route reading the
    `device` query param directly instead of through that shared
    guard, which would silently 200 on a stale or typo'd device id."""
    application, _device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/energy", params={"device": 9999})
        assert response.status_code == 404
    finally:
        application.database.close()
