"""Integration tests for SEC-07 (orchestrator finding, not in either QA
report): design D11 (`storage/state.py`'s module docstring, confirmed
against Engram design-edges obs 5706's "Session" section) requires that
restarting the process must not revoke sessions, while changing the
password (or calling `/logout`) must. Built through the real
`create_app`/`bootstrap.build` composition root, called *twice* against
the same on-disk database to simulate a restart -- the same technique
`tests/integration/storage/test_state.py` already uses for the secret
alone, extended here to the full login/logout flow so the wiring is
proven in production code, not just in `storage/state.py`'s own
pure-ish functions.
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
from ecoflow_stats.web.security import CSRF_COOKIE, SESSION_COOKIE, csrf_token
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_PASSWORD = "correct-password-1"
_LOCAL_CLIENT = ("127.0.0.1", 12345)


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
        "ECOFLOW_STATS_PASSWORD": _PASSWORD,
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


def _csrf_headers(client: TestClient, application: bootstrap.Application) -> dict[str, str]:
    """A GET first mints (or reuses) the real `efs_csrf` cookie, exactly
    like the rendered `login.html`/the logged-in pages provide; reusing
    the existing cookie value (`csrf_cookie_value`'s fall-through) keeps
    this safe to call more than once per session."""
    client.get("/login")
    return {"x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])}


def test_a_session_survives_a_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pass-1 (SEC-07): before this fix, `Application.secret` was fresh
    random bytes on every `bootstrap.build` call (its own docstring
    claimed this was intentional), so a session cookie signed by one
    running process could never verify against the next one -- an
    ordinary restart (a deploy, a crash, a reboot) silently logged out
    the one real user, contrary to design D11.

    Pass-2 target: reverting `bootstrap.build`'s `secret=` back to
    `secrets.token_bytes(32)` turns this red -- the second
    `Application` gets a different, unrelated secret and this same
    cookie fails to verify, landing on the login redirect instead of
    the page."""
    app1 = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(app1), client=_LOCAL_CLIENT, follow_redirects=False) as client:
            login = client.post(
                "/login",
                data={"password": _PASSWORD, "next": "/"},
                headers=_csrf_headers(client, app1),
            )
            assert login.status_code == 303
            cookie = client.cookies[SESSION_COOKIE]
    finally:
        app1.database.close()

    # Simulate a restart: a brand new process re-reads the same on-disk
    # database and builds a brand new `Application` from scratch.
    app2 = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(app2), client=_LOCAL_CLIENT, follow_redirects=False) as client:
            client.cookies.set(SESSION_COOKIE, cookie)
            response = client.get("/")
        assert response.status_code == 200
    finally:
        app2.database.close()


def test_a_logged_out_session_stays_revoked_after_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (SEC-07): fixing the secret alone (making it persistent)
    would, on its own, accidentally *resurrect* every session `/logout`
    had already revoked -- `SecurityContext.session_generation` resets
    to its default `0` on every fresh `Application` unless it is also
    persisted and restored, which would make a cookie revoked by
    bumping it to `1` start verifying again the moment a restart
    quietly resets the comparison value back to `0`.

    Pass-2 target: reverting the new `set_session_generation(...)` call
    this fix adds to `logout_submit` (or `bootstrap.build`'s matching
    `get_session_generation(...)` read) turns this red -- the
    pre-logout cookie authenticates again against the second, restarted
    `Application`."""
    app1 = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(app1), client=_LOCAL_CLIENT, follow_redirects=False) as client:
            client.post(
                "/login",
                data={"password": _PASSWORD, "next": "/"},
                headers=_csrf_headers(client, app1),
            )
            cookie = client.cookies[SESSION_COOKIE]
            logout = client.post("/logout", headers=_csrf_headers(client, app1))
            assert logout.status_code == 303
    finally:
        app1.database.close()

    app2 = _build(monkeypatch, tmp_path)
    try:
        with TestClient(_app(app2), client=_LOCAL_CLIENT, follow_redirects=False) as client:
            client.cookies.set(SESSION_COOKIE, cookie)
            response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
    finally:
        app2.database.close()
