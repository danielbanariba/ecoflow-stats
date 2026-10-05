"""Integration tests for `create_app`: the FastAPI composition shell that
starts the collector under supervision via its lifespan and wires a real
`HealthContext` from the `Application` `bootstrap.build` returns.
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
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0001",
}


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _application(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> bootstrap.Application:
    env = {**VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data")}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    return bootstrap.build(settings, clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))


def _never_ticks(application: bootstrap.Application) -> SupervisedTaskHandle:
    """A stand-in collector that starts, then blocks forever instead of
    ever fetching anything. Proves the real asyncio lifecycle (start on
    startup, cancel-and-await on shutdown) without a real network call and
    without a tight loop driven by a clock that never really waits."""

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-collector", target=_blocks_forever, clock=application.clock
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def test_lifespan_starts_the_collector_on_startup_and_cancels_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(monkeypatch, tmp_path)
    holder: dict[str, SupervisedTaskHandle] = {}

    def start_collector(app: bootstrap.Application) -> SupervisedTaskHandle:
        handle = _never_ticks(app)
        holder["handle"] = handle
        return handle

    app = create_app(application, start_collector=start_collector)

    with TestClient(app):
        assert holder["handle"].alive is True

    assert holder["handle"].alive is False
    application.database.close()


def test_healthz_is_reachable_through_the_real_app_and_reports_the_seeded_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(monkeypatch, tmp_path)
    app = create_app(application, start_collector=_never_ticks)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "starting"  # no sample collected yet
    assert [d["id"] for d in body["devices"]] == [application.device_records[0].id]
    application.database.close()
