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
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.security import CSRF_COOKIE, csrf_token
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


def test_the_login_page_renders_spanish_translations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-06 (qa-report-ui-01.md): the login page's own title, password
    label, and submit button were hardcoded English strings, so a
    Spanish-negotiated visitor saw a page that was otherwise translated
    except for the one page they hit before authenticating.

    Pass-2 target: reverting `login.html` to the literal "Log in" /
    "Password" strings (instead of `{{ t('login.title') }}` /
    `{{ t('login.password_label') }}` / `{{ t('login.submit_button') }}`)
    turns this red."""
    client, application = _app(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get("/login", headers={"accept-language": "es-HN,es;q=0.9"})

        assert response.status_code == 200
        assert "Iniciar sesión" in response.text
        assert "Contraseña" in response.text
        assert "Log in" not in response.text
        assert "Password" not in response.text
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


def test_the_htmx_error_banner_is_rendered_hidden_with_translated_accessible_markup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-15 (qa-report-ui-01.md): "a failed HTMX mutating request shows
    no feedback". `app.js` has no server call of its own to fetch a
    translated string from, so this banner's text must already be
    baked into the server-rendered page in the negotiated language,
    exactly like every other translated string on the page -- before
    this fix, no page rendered any element at all for `app.js` to show
    feedback in, matching the finding exactly.

    Pass-2 target: removing `#htmx-error-banner`'s `role`, `aria-live`,
    or `hidden` attribute from `base.html`, or its translated
    `data-response-message`/`data-send-message` values, turns this
    red."""
    client, application = _app(monkeypatch, tmp_path)
    try:
        with client:
            en_response = client.get("/")
            es_response = client.get("/", headers={"accept-language": "es-HN,es;q=0.9"})

        cases = (
            (en_response, "Something went wrong submitting that.", "Could not reach the server."),
            (
                es_response,
                "Ocurrió un error al enviar eso.",
                "No se pudo conectar con el servidor.",
            ),
        )
        for response, expected_response_msg, expected_send_msg in cases:
            assert response.status_code == 200
            html = response.text
            match = re.search(r'<div[^>]*id="htmx-error-banner"[^>]*>', html)
            assert match is not None, "no #htmx-error-banner element rendered"
            tag = match.group(0)
            assert 'role="alert"' in tag
            assert 'aria-live="assertive"' in tag
            assert "hidden" in tag
            assert expected_response_msg in tag
            assert expected_send_msg in tag
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
            csrf_header = {
                "x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])
            }
            preferences_response = client.post(
                "/preferences",
                data={"lang": "en"},
                headers={**csrf_header, "accept-language": "es-HN"},
            )
            assert preferences_response.status_code == 200  # redirect followed, not a 403
            response = client.get("/", headers={"accept-language": "es-HN,es;q=0.9"})

        assert 'lang="en"' in response.text
        assert "Overview" in response.text
    finally:
        application.database.close()
