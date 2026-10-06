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
from ecoflow_stats.web import app as web_app
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

    app = create_app(application, start_collector=start_collector, start_derive_job=_never_ticks)

    with TestClient(app):
        assert holder["handle"].alive is True

    assert holder["handle"].alive is False
    application.database.close()


def test_lifespan_starts_the_derive_job_on_startup_and_cancels_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without this, the 5-minute derive job (`jobs.run_derive_forever`)
    is fully implemented and tested in isolation but never actually runs
    in the real server process -- a device fed only by the live collector
    would accumulate dirty samples that nothing ever recomputes (batch
    7's documented known gap: "jobs.run_derive_forever is also not yet
    wired into web/app.py's lifespan")."""
    application = _application(monkeypatch, tmp_path)
    holder: dict[str, SupervisedTaskHandle] = {}

    def start_derive_job(app: bootstrap.Application) -> SupervisedTaskHandle:
        handle = _never_ticks(app)
        holder["handle"] = handle
        return handle

    app = create_app(application, start_collector=_never_ticks, start_derive_job=start_derive_job)

    with TestClient(app):
        assert holder["handle"].alive is True

    assert holder["handle"].alive is False
    application.database.close()


def test_healthz_is_reachable_through_the_real_app_and_reports_the_seeded_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _application(monkeypatch, tmp_path)
    app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "starting"  # no sample collected yet
    assert [d["id"] for d in body["devices"]] == [application.device_records[0].id]
    application.database.close()


def test_the_default_derive_job_is_started_with_the_applications_own_devices_and_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proves the production wiring point (not just an injectable test
    double): `create_app`'s real default must hand `run_derive_forever`
    this exact application's device ids, database and clock -- the same
    way `_start_collector`'s default already does for the collector."""
    application = _application(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_run_derive_forever(device_ids: object, **kwargs: object) -> None:
        captured["device_ids"] = device_ids
        captured["database"] = kwargs["database"]
        captured["clock"] = kwargs["clock"]
        await asyncio.Event().wait()

    monkeypatch.setattr(web_app, "run_derive_forever", _fake_run_derive_forever)

    app = create_app(application, start_collector=_never_ticks)

    with TestClient(app):
        pass

    assert captured["device_ids"] == tuple(r.id for r in application.device_records)
    assert captured["database"] is application.database
    assert captured["clock"] is application.clock
    application.database.close()


def test_the_default_collector_is_started_with_the_applications_own_live_state_and_notifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without this, the real collector would run with no live detector
    state and no notification service even when one was built -- every
    below-threshold reading would be judged against a fresh, empty
    window every tick instead of the application's own replayed state,
    and no live transition would ever reach a notifier."""
    monkeypatch.setenv("ECOFLOW_STATS_NTFY_TOPIC", "test-topic")
    application = _application(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def _fake_run_forever(devices: object, **kwargs: object) -> None:
        # `.get(...)`, never `[...]`, so a not-yet-wired kwarg is captured
        # as `None` instead of raising -- an exception here would retry
        # in a tight loop against this test's `FakeClock` (whose
        # `sleep_until` never really waits), hanging the test instead of
        # failing it cleanly.
        captured["live_states"] = kwargs.get("live_states")
        captured["notification_service"] = kwargs.get("notification_service")
        await asyncio.Event().wait()

    monkeypatch.setattr(web_app, "run_forever", _fake_run_forever)

    app = create_app(application, start_derive_job=_never_ticks)

    with TestClient(app):
        pass

    assert captured["live_states"] is application.live_outage_states
    assert captured["notification_service"] is application.notification_service
    application.database.close()
