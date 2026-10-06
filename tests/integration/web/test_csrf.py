"""Integration tests for CSRF protection (access-control design, "CSRF";
Named Defect "CSRF gap") and the security response headers (design,
"Headers"), built through the real `create_app`/`bootstrap.build`
composition root the same way `tests/integration/web/test_access_lan.py`
already does.

The enumeration test walks the real `app.routes` rather than hardcoding a
path list, so a future POST route that forgets `require_csrf` is caught
automatically (the brief: "find them all, don't hardcode a guess").
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.security import CSRF_COOKIE, csrf_token, require_csrf
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


def _iter_api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    """Recurse into FastAPI's lazily-included sub-routers so every real
    `APIRoute` is visited, not just the top-level ones directly on
    `app.routes` (this FastAPI version wraps `include_router` targets in
    an internal `_IncludedRouter` rather than flattening them eagerly)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner_router = getattr(route, "original_router", None)
        if inner_router is not None:
            yield from _iter_api_routes(inner_router.routes)


def _post_routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in _iter_api_routes(app.routes) if "POST" in route.methods]


def test_every_registered_post_route_requires_csrf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Named Defect "CSRF gap": every POST route this app registers must
    carry the `require_csrf` dependency. Catches a future POST route
    (or a refactor of an existing one) that forgets to attach it."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)
        post_routes = _post_routes(app)
        paths = {route.path for route in post_routes}
        # Sanity: the walk actually found real routes, not an empty list
        # that would make the assertion below trivially (and uselessly) pass.
        assert {"/preferences", "/login", "/logout"} <= paths

        missing = [
            route.path
            for route in post_routes
            if not any(dep.call is require_csrf for dep in route.dependant.dependencies)
        ]

        assert missing == []
    finally:
        application.database.close()


def test_a_cross_site_origin_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Design, "CSRF": "rejects a present Origin whose host differs from
    Host" -- the signal every modern browser sends on a genuine
    cross-site POST, independent of whether any CSRF token is present."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.post(
                "/preferences",
                data={"lang": "en"},
                headers={"origin": "http://evil.example"},
            )

        assert response.status_code == 403
    finally:
        application.database.close()


def test_a_cross_site_sec_fetch_site_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Design, "CSRF": "rejects a Sec-Fetch-Site other than
    same-origin/none" -- the Fetch Metadata header a modern browser sends
    on every request, including one driven entirely by an attacker page."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.post(
                "/preferences",
                data={"lang": "en"},
                headers={"sec-fetch-site": "cross-site"},
            )

        assert response.status_code == 403
    finally:
        application.database.close()


def test_a_mismatched_csrf_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proves the double-submit match itself is load-bearing: once a page
    has handed out a real `efs_csrf` cookie, a request that submits a
    token not derived from that exact cookie is rejected -- this is the
    actual forgery case a copied-but-wrong, or simply guessed, token
    represents."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            client.get("/")  # mints the real efs_csrf cookie
            assert CSRF_COOKIE in client.cookies

            response = client.post(
                "/preferences",
                data={"lang": "en"},
                headers={"x-csrf-token": "not-the-real-token"},
            )

        assert response.status_code == 403
    finally:
        application.database.close()


def test_a_matching_double_submit_csrf_token_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A same-origin POST with the correct double-submit CSRF
    token succeeds": the exact token a legitimate page would compute from
    the real cookie, submitted as `X-CSRF-Token`, is accepted."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app, follow_redirects=False) as client:
            client.get("/")
            cookie_value = client.cookies[CSRF_COOKIE]
            correct_token = csrf_token(application.secret, cookie_value)

            response = client.post(
                "/preferences",
                data={"lang": "en"},
                headers={"x-csrf-token": correct_token},
            )

        assert response.status_code == 303
        assert response.headers["set-cookie"].lower().count("efs_csrf") in (0, 1)
    finally:
        application.database.close()


def test_the_csp_header_disallows_inline_scripts_and_unsafe_eval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Design, "Headers": the CSP keeps `script-src` to `'self'` only --
    no `'unsafe-inline'`, no `'unsafe-eval'` -- consistent with slice 20's
    "every script is external" design (`htmx-config`'s `allowEval:
    false`); a regression here would silently reopen inline-script
    execution for any future injected HTML."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)

        with TestClient(app) as client:
            response = client.get("/")

        csp = response.headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
    finally:
        application.database.close()


def test_the_csp_header_is_present_even_on_a_denied_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The header middleware wraps `AccessControlMiddleware`, not just the
    page routes -- a LAN-guard 403 must carry the same headers as a
    normal page, since it serves real HTML to a real browser too."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = _app(application)
        outside_lan = ("203.0.113.5", 12345)  # RFC 5737 TEST-NET-3

        with TestClient(app, client=outside_lan) as client:
            response = client.get("/")

        assert response.status_code == 403
        assert "content-security-policy" in response.headers
    finally:
        application.database.close()
