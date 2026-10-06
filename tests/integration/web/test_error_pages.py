"""Integration tests for UI-13 (qa-report-ui-01.md): a page route's
`404` and `500` must render a designed, translated HTML error page --
not FastAPI's raw JSON `{"detail": ...}` body, and not a bare
`Internal Server Error` plain-text body -- while `/api/v1/*` keeps its
existing JSON/plain-text error bodies unchanged. Built through the
real `create_app`/`bootstrap.build` composition root, the same way
`tests/integration/web/test_security_headers.py` already does, so the
handler wiring in `web.app.create_app` and
`web.security.SecurityHeadersMiddleware` is proven end to end, not
just the `web.routes.errors.render_error_page` function in isolation.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.routes import pages as pages_module
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-collector", target=_blocks_forever, clock=application.clock
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def _app(application: bootstrap.Application) -> FastAPI:
    return create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )


def test_a_404_on_a_page_route_renders_the_designed_html_error_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (UI-13): before this fix, an unknown page route (anything
    not under `/api/`) returned FastAPI's raw JSON `{"detail": "Not
    Found"}` -- the same bare body a non-browser API client gets,
    carrying none of this app's own design, language, or navigation.

    Pass-2 target: dropping `create_app`'s new
    `app.add_exception_handler(StarletteHTTPException,
    _handle_http_exception)` registration turns this red -- the
    response would be `application/json` with a `detail` key instead
    of HTML containing the translated title."""
    application = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(application)) as client:
            response = client.get("/this-page-does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert "Page not found" in response.text
    finally:
        application.database.close()


def test_an_unknown_device_query_param_on_a_page_route_renders_the_404_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API-02 (qa-report-data-01.md), the page half: when API-02 was
    first fixed (batch B), only `/api/v1/*` routes were in scope -- a
    page route's own `?device=<unknown>` silently fell back to the
    first configured device instead of telling the visitor their link
    was wrong. Pass-1: a page route still swallowing an unknown
    `device` id would show device 1's data under a URL that claims to
    be about a device that does not exist -- this proves it now
    renders the same themed 404 `test_a_404_on_a_page_route_renders_
    the_designed_html_error_page` already proves for an unknown path,
    reusing `web.routes.errors.render_error_page` rather than a
    second, divergent not-found page."""
    application = _build(monkeypatch, tmp_path)
    try:
        (configured_device,) = application.device_records
        unknown_device_id = configured_device.id + 999
        with TestClient(_app(application)) as client:
            response = client.get(f"/?device={unknown_device_id}")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert "Page not found" in response.text
    finally:
        application.database.close()


def test_a_404_on_an_api_route_still_returns_the_existing_json_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: UI-13's fix must only change page routes --
    `/api/v1/*` keeps its existing JSON error body unchanged, so a
    non-browser client never has to parse HTML to detect a `404`."""
    application = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(application)) as client:
            response = client.get("/api/v1/this-route-does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["detail"]
    finally:
        application.database.close()


def test_the_404_page_is_translated_by_the_lang_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-13 explicitly requires a *translated* page -- proven here
    against the Spanish catalog, the same `lang` cookie every other
    page already negotiates with."""
    application = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(application)) as client:
            client.cookies.set("lang", "es")
            response = client.get("/this-page-does-not-exist")

        assert response.status_code == 404
        assert "Página no encontrada" in response.text
    finally:
        application.database.close()


def test_the_404_page_keeps_security_and_cache_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard (batch A, SEC-05/SEC-06): the new HTML error
    page must not bypass `SecurityHeadersMiddleware` -- it is still
    just a `Response` returned from inside the same middleware stack,
    but this proves the exception-handler path specifically, which
    batch A's own crash-catch tests never exercised."""
    application = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(application)) as client:
            response = client.get("/this-page-does-not-exist")

        assert "content-security-policy" in response.headers
        assert response.headers.get("cache-control") == "no-store"
    finally:
        application.database.close()


def test_an_unhandled_exception_on_a_page_route_renders_the_designed_html_error_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (UI-13, the `500` half): before this fix, a crashed page
    route fell all the way through to `SecurityHeadersMiddleware`'s own
    crash-catch (SEC-05), which returned a bare `Internal Server
    Error` plain-text body -- carrying this app's security headers
    (SEC-05's own fix), but still no design or language.

    Pass-2 target: reverting `SecurityHeadersMiddleware.dispatch`'s new
    non-`/api/` branch (falling back to the old unconditional
    `PlainTextResponse("Internal Server Error", 500)`) turns this red
    -- the body would be plain text, not HTML containing the
    translated title."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(pages_module, "get_status", _boom)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("text/html")
        assert "Something went wrong" in response.text
        assert "content-security-policy" in response.headers
    finally:
        application.database.close()


def test_an_unhandled_exception_on_an_api_route_still_returns_plain_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: UI-13's `500` fix must not change `/api/v1/*`'s
    existing crash body (SEC-05, already proven by
    `test_security_headers.py`) -- this locks the dichotomy from the
    other side, through this file's own harness."""
    application = _build(monkeypatch, tmp_path)
    try:
        from ecoflow_stats.web.routes import api as api_module

        app = _app(application)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(api_module, "get_status", _boom)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/status")

        assert response.status_code == 500
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == "Internal Server Error"
    finally:
        application.database.close()
