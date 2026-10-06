"""Integration tests for the LAN guard (access-control requirement:
"Unauthenticated by Default, With a Startup Warning"), built through the
real `create_app`/`bootstrap.build` composition root — the same way
`tests/integration/web/test_overview_page.py` already does — so the
guard is proven wired end-to-end, not just against a hand-built stub.

The simulated client address comes from `TestClient(..., client=(host,
port))`, Starlette's own documented mechanism for setting the ASGI
scope's peer address: https://www.starlette.io/testclient/.
"""

from __future__ import annotations

import logging
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

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_OUTSIDE_LAN = ("203.0.113.5", 12345)  # RFC 5737 TEST-NET-3: never a private range
_INSIDE_LAN = ("127.0.0.1", 12345)  # within the default allowed-networks loopback range


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


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


def _never_ticks(application: bootstrap.Application) -> SupervisedTaskHandle:
    import asyncio

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-collector", target=_blocks_forever, clock=application.clock
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def test_a_client_outside_the_allowed_networks_is_denied_with_a_403_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A client outside ECOFLOW_STATS_ALLOWED_NETWORKS gets
    403 with a page directing them to set a password" — no password is
    configured, so the LAN guard is the only protection in effect."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app, client=_OUTSIDE_LAN) as client:
            response = client.get("/")

        assert response.status_code == 403
        assert "password" in response.text.lower()
        assert "live-card" not in response.text  # never the actual page content
    finally:
        application.database.close()


def test_an_allowed_range_client_is_served_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "an allowed-range client is served normally"."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app, client=_INSIDE_LAN) as client:
            response = client.get("/")

        assert response.status_code == 200
    finally:
        application.database.close()


def test_the_health_check_stays_reachable_from_outside_the_allowed_networks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "no password means open access" is bounded by "every
    route *other than the health check*" — proves the LAN guard never
    blocks `/healthz` even for an address the guard would otherwise
    reject, so a container orchestrator's health probe is never
    coupled to where it happens to run."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app, client=_OUTSIDE_LAN) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
    finally:
        application.database.close()


def test_no_password_configured_emits_a_visible_startup_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Scenario "No password means open access and a warning"."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with caplog.at_level(logging.WARNING), TestClient(app, client=_INSIDE_LAN):
            pass

        assert "no password" in caplog.text.lower()
    finally:
        application.database.close()
