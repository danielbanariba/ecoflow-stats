"""Integration tests for security/cache response headers on EVERY
response, including an unhandled exception (QA report
`qa-report-data-01.md`, findings SEC-05, SEC-06, POLISH-01), built
through the real `create_app`/`bootstrap.build` composition root the
same way `tests/integration/web/test_csrf.py` already does.
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
from ecoflow_stats.web.routes import api as api_module
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


def _app(application: bootstrap.Application) -> FastAPI:
    return create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )


def test_security_headers_are_present_on_an_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (SEC-05): the QA report found the 500 response from a
    crashed route carrying none of the app's security headers --
    `SecurityHeadersMiddleware`'s header-setting loop never runs for a
    response built by the exception path, which previously bypassed it
    entirely. Deliberately not reusing API-01's now-fixed overflow crash
    -- this must hold for *any* unhandled exception, not just the one
    this batch already patched, so `get_status` (the status route's own
    dependency, untouched by any other fix in this batch) is forced to
    raise directly."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(api_module, "get_status", _boom)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/status")

        assert response.status_code == 500
        assert "content-security-policy" in response.headers
        assert response.headers.get("x-content-type-options") == "nosniff"
    finally:
        application.database.close()


def test_the_unhandled_exception_body_still_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: the fix for SEC-05 must not turn an unhandled
    exception into a debug page or a leaked traceback -- the QA report's
    own "Verified correct" finding that the crash body never leaks
    internals must keep holding once the handler is added."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("a secret implementation detail")

        monkeypatch.setattr(api_module, "get_status", _boom)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/status")

        assert "a secret implementation detail" not in response.text
        assert "Traceback" not in response.text
    finally:
        application.database.close()


def test_cache_control_no_store_is_present_on_a_page_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (SEC-06): combined with SEC-03 (session not revoked on
    logout), a cacheable authenticated page risks a shared machine's
    browser back/forward cache still displaying it after logout."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.get("/")

        assert response.headers.get("cache-control") == "no-store"
    finally:
        application.database.close()


def test_cache_control_no_store_is_present_on_an_api_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (SEC-06): the finding names "authenticated pages and API
    responses" explicitly -- the JSON API must carry the same
    no-cache guarantee as the HTML pages."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.get("/api/v1/status")

        assert response.headers.get("cache-control") == "no-store"
    finally:
        application.database.close()


def test_cache_control_no_store_is_not_forced_onto_static_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a blanket `no-store` on every single response would also
    defeat the browser's cache for `/static/*` (CSS/JS/vendor files),
    which is not what the finding asks for and would be a real
    performance regression -- `/static/*` must be exempted from the
    page/API `no-store` rule."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.get("/static/app.css")

        assert response.status_code == 200
        assert response.headers.get("cache-control") != "no-store"
    finally:
        application.database.close()


def test_x_frame_options_deny_is_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pass-1 (POLISH-01): defense-in-depth alongside the existing CSP
    `frame-ancestors 'none'`, for a legacy browser that does not honor
    `frame-ancestors`."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.get("/")

        assert response.headers.get("x-frame-options") == "DENY"
    finally:
        application.database.close()
