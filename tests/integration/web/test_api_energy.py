"""Integration tests for the `GET /api/v1/energy/daily` read-only API
route (task 19.1/19.2's API half): daily (or monthly beyond 90 days)
kWh by flow, cost per period and total, and each row's estimate flags.

A standalone `FastAPI()` with only the api router mounted and
`app.state.api` set directly to a hand-built `ApiContext` against real
SQLite stores -- mirrors `test_api_grid.py`'s own harness. One test
instead builds the real `create_app`/`bootstrap.build` composition root
and drives the actual `derive_rollups` function, proving this batch's
energy-reading route serves real persisted data, not just a
hand-seeded row (the "zero production callers" gap class).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.web.routes.api import ApiContext, router

_NOW = datetime(2026, 4, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


def _client(
    tmp_path: Path, *, tariff: float | None = None, currency: str = ""
) -> tuple[TestClient, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    db.writer.commit()

    app = FastAPI()
    app.state.api = ApiContext(
        now=lambda: _NOW,
        stale_threshold_s=180,
        poll_interval_s=60,
        detector_config=DetectorConfig(),
        sample_store=None,  # type: ignore[arg-type]
        outage_store=None,  # type: ignore[arg-type]
        device_records=(record,),
        tz="UTC",
        tariff=tariff,
        currency=currency,
    )
    app.state.application = SimpleNamespace(database=db)
    app.include_router(router)
    return TestClient(app), db, record.id


def _seed_energy_day(
    db: Database, device_id: int, day: str, *, chg_ac_wh: float, energy_flags: int = 0
) -> None:
    RollupStore(db.writer).upsert_energy(
        device_id,
        day,
        chg_ac_wh=chg_ac_wh,
        chg_dc_wh=0.0,
        chg_solar_wh=0.0,
        dsg_ac_wh=0.0,
        dsg_dc_wh=0.0,
        chg_ac_est_wh=0.0,
        energy_flags=energy_flags,
    )
    db.writer.commit()


def test_energy_daily_returns_daily_periods_with_flags_for_a_range_of_90_days_or_less(
    tmp_path: Path,
) -> None:
    """Task 19.1's granularity rule, within budget. Pass-1: catches the
    route aggregating into months when a daily breakdown was asked
    for, or dropping the per-row `counter_reset`/etc. flags."""
    client, db, device_id = _client(tmp_path)
    _seed_energy_day(db, device_id, "2026-04-09", chg_ac_wh=500.0, energy_flags=1)

    response = client.get(
        "/api/v1/energy/daily", params={"from": _NOW_TS - 2 * 86_400, "to": _NOW_TS}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["granularity"] == "daily"
    period = next(p for p in body["periods"] if p["period"] == "2026-04-09")
    assert period["chg_ac_wh"] == 500.0
    assert period["flags"] == ["counter_reset"]


def test_energy_daily_aggregates_into_monthly_periods_beyond_90_days(tmp_path: Path) -> None:
    """Task 19.1's granularity rule, over budget. Pass-1: catches a
    defect that keeps daily rows (or picks the wrong month key) once
    the requested range exceeds 90 days, which would explode the
    response size for a long-lived device."""
    client, db, device_id = _client(tmp_path)
    _seed_energy_day(db, device_id, "2026-01-05", chg_ac_wh=100.0)
    _seed_energy_day(db, device_id, "2026-01-20", chg_ac_wh=50.0, energy_flags=2)

    response = client.get(
        "/api/v1/energy/daily", params={"from": _NOW_TS - 100 * 86_400, "to": _NOW_TS}
    )

    body = response.json()
    assert body["granularity"] == "monthly"
    period = next(p for p in body["periods"] if p["period"] == "2026-01")
    assert period["chg_ac_wh"] == 150.0
    assert period["flags"] == ["implausible_jump"]


def test_energy_daily_reports_cost_as_unavailable_without_a_configured_tariff(
    tmp_path: Path,
) -> None:
    """Scenario "No configured tariff shows energy without a fabricated
    cost". Pass-1: catches cost silently defaulting to `0` instead of
    the honest `"unavailable"` sentinel."""
    client, db, device_id = _client(tmp_path, tariff=None)
    _seed_energy_day(db, device_id, "2026-04-09", chg_ac_wh=500.0)

    response = client.get(
        "/api/v1/energy/daily", params={"from": _NOW_TS - 2 * 86_400, "to": _NOW_TS}
    )

    body = response.json()
    period = next(p for p in body["periods"] if p["period"] == "2026-04-09")
    assert period["cost"] == "unavailable"
    assert body["total"]["cost"] == "unavailable"


def test_energy_daily_computes_cost_from_the_configured_tariff(tmp_path: Path) -> None:
    """Scenario "Cost is price times AC charge-in energy". Pass-1:
    catches the wrong cost formula (missing the Wh-to-kWh conversion,
    or using the wrong counter)."""
    client, db, device_id = _client(tmp_path, tariff=0.20, currency="USD")
    _seed_energy_day(db, device_id, "2026-04-09", chg_ac_wh=2_000.0)  # 2 kWh

    response = client.get(
        "/api/v1/energy/daily", params={"from": _NOW_TS - 2 * 86_400, "to": _NOW_TS}
    )

    body = response.json()
    period = next(p for p in body["periods"] if p["period"] == "2026-04-09")
    assert period["cost"] == pytest.approx(0.40)
    assert body["total"]["currency"] == "USD"


# --- end-to-end: the real derive path persists, the real route serves --


def _never_ticks(application: object) -> object:
    from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-job",
        target=_blocks_forever,
        clock=application.clock,  # type: ignore[attr-defined]
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def test_the_real_derive_path_persists_energy_fields_the_api_then_serves_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gap class flagged twice before in this project (derive_
    rollups and the energy service each once had zero production
    callers). This test seeds raw counter samples, runs the real
    `rollups.service.derive_rollups`, then hits the real `GET
    /api/v1/energy/daily` route through a `TestClient` built from the
    real composition root with a configured tariff, proving the kWh
    and cost the API returns are what the real derive path actually
    computed and persisted."""
    from ecoflow_stats import bootstrap
    from ecoflow_stats.config import load_settings
    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.rollups.service import derive_rollups
    from ecoflow_stats.storage.derivations import DerivationStore
    from ecoflow_stats.storage.samples import SampleStore
    from ecoflow_stats.web.app import create_app
    from tests.fakes import FakeClock

    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
        "ECOFLOW_STATS_TARIFF": "0.25",
        "ECOFLOW_STATS_CURRENCY": "USD",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    now = datetime.fromtimestamp(_NOW_TS, tz=UTC)
    application = bootstrap.build(settings, clock=FakeClock(now))
    (device,) = application.device_records

    sample_store = SampleStore(application.database.writer)
    sample_store.add(device.id, _NOW_TS - 3_600, 1, Reading(chg_ac_wh=0.0))
    sample_store.add(device.id, _NOW_TS - 60, 1, Reading(chg_ac_wh=1_000.0))  # +1 kWh
    application.database.writer.commit()

    application.database.writer.execute("BEGIN IMMEDIATE")
    derive_rollups(
        device.id,
        sample_store=sample_store,
        rollup_store=RollupStore(application.database.writer),
        derivation_store=DerivationStore(application.database.writer),
        now=now,
    )
    application.database.writer.commit()

    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/energy/daily", params={"from": _NOW_TS - 86_400, "to": _NOW_TS}
        )

    body = response.json()
    today = next(p for p in body["periods"] if p["chg_ac_wh"] == pytest.approx(1_000.0))
    assert today["cost"] == pytest.approx(0.25)
    assert body["total"]["currency"] == "USD"
