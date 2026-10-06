"""Integration tests for browser language negotiation on a real page
(web-ui "Language Negotiated From the Browser, With English Fallback";
"Manual Language Override Persists").

Unlike `tests/unit/web/test_i18n.py` (the pure negotiation/lookup rule
in isolation), these drive the real `GET /` route and the real
`POST /preferences` route through the full composition root.
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

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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


def _app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, bootstrap.Application]:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    application = bootstrap.build(settings, clock=FakeClock(_NOW))
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    return TestClient(app), application


def test_a_spanish_browser_preference_renders_spanish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, application = _app(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/", headers={"accept-language": "es-HN,es;q=0.9,en;q=0.5"})

        assert response.status_code == 200
        assert 'lang="es-419"' in response.text
        assert "Resumen" in response.text  # Spanish for "Overview"
    finally:
        application.database.close()


def test_an_unsupported_preference_falls_back_to_english(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, application = _app(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/", headers={"accept-language": "fr-FR,de;q=0.8"})

        assert response.status_code == 200
        assert 'lang="en"' in response.text
        assert "Overview" in response.text
    finally:
        application.database.close()


def test_a_manual_override_replaces_the_negotiated_language_for_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A manual override replaces the negotiated language for
    the session": negotiate Spanish, then manually select English, then
    request subsequent pages — they render English, not Spanish."""
    client, application = _app(monkeypatch, tmp_path)
    try:
        with client:
            client.get("/", headers={"accept-language": "es-HN,es;q=0.9"})
            client.post("/preferences", data={"lang": "en"}, headers={"accept-language": "es-HN"})
            response = client.get("/", headers={"accept-language": "es-HN,es;q=0.9"})

        assert 'lang="en"' in response.text
        assert "Overview" in response.text
    finally:
        application.database.close()
