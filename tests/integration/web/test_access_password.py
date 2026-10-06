"""Integration tests for the optional password-protected session
(access-control requirement: "Optional Password Guards Every Route
Except the Health Check"), built through the real
`create_app`/`bootstrap.build` composition root — the same way
`tests/integration/web/test_access_lan.py` already does — so the guard
is proven wired end-to-end, not just against a hand-built stub.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.security import (
    LoginThrottle,
    issue_session_token,
    session_signing_key,
    verify_session_token,
)
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_PASSWORD = "correct-password-1"
_LOCAL_CLIENT = ("127.0.0.1", 12345)


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, password: str | None = _PASSWORD
) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    if password is not None:
        env["ECOFLOW_STATS_PASSWORD"] = password
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


def test_an_unauthenticated_request_to_a_protected_route_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "An unauthenticated request to a protected route is
    rejected": a challenge/redirect, never the content."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            response = client.get("/")

        assert response.status_code == 303
        assert "live-card" not in response.text
    finally:
        application.database.close()


def test_the_health_check_remains_reachable_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "The health check remains reachable without
    credentials"."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
    finally:
        application.database.close()


def test_the_correct_password_is_accepted_and_grants_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "The correct password is accepted"."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT) as client:
            login_response = client.post("/login", data={"password": _PASSWORD, "next": "/"})
            assert login_response.status_code == 200  # followed the redirect to "/"

            home_response = client.get("/")

        assert home_response.status_code == 200
        assert "live-card" in home_response.text or "No data yet" in home_response.text
    finally:
        application.database.close()


def test_a_wrong_password_grants_no_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            client.post("/login", data={"password": "totally-wrong", "next": "/"})
            response = client.get("/")

        assert response.status_code == 303  # still no session
    finally:
        application.database.close()


def test_session_cookie_is_httponly_and_samesite_lax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Design, "Session": `HttpOnly` so script-injected JS cannot read
    the cookie; `SameSite=Lax` so it rides along a same-site navigation
    but not an arbitrary cross-site request."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            response = client.post("/login", data={"password": _PASSWORD, "next": "/"})

        set_cookie = response.headers.get("set-cookie", "").lower()
        assert "efs_session=" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
    finally:
        application.database.close()


def test_a_wrong_password_attempt_is_really_delayed_by_the_real_login_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proves the real production wiring (not just the pure
    `LoginThrottle` class below) actually delays on a failed attempt —
    design, "Password set": "a login throttle delays 1 s per failure"."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT) as client:
            started = time.monotonic()
            client.post("/login", data={"password": "totally-wrong", "next": "/"})
            elapsed = time.monotonic() - started

        assert elapsed >= 1.0
    finally:
        application.database.close()


def test_a_password_change_invalidates_every_previously_issued_session() -> None:
    """Design, "Session": the session signing key is `HMAC(app_secret,
    sha256(password))`, so changing the password alone revokes every
    session signed under the old one."""
    secret = b"a" * 32
    old_key = session_signing_key(secret, "old-password-123")
    new_key = session_signing_key(secret, "new-password-456")
    token = issue_session_token(old_key, issued_at=0, expires_at=1_000_000)

    assert verify_session_token(old_key, token, now=100) is True
    assert verify_session_token(new_key, token, now=100) is False


def test_login_throttle_delays_one_second_per_failure_and_caps_at_ten_in_five_minutes() -> None:
    """Design, "Password set": "a login throttle delays 1 s per failure
    and at most 10 failures per 5 minutes per client address"."""
    sleeps: list[float] = []
    clock = {"t": 0.0}
    throttle = LoginThrottle(now=lambda: clock["t"], sleep=sleeps.append)

    for _ in range(10):
        assert throttle.allow("1.2.3.4") is True
        throttle.record_failure("1.2.3.4")

    assert sleeps == [1.0] * 10
    assert throttle.allow("1.2.3.4") is False  # the 11th attempt is capped

    clock["t"] += 301  # advance past the 5-minute window
    assert throttle.allow("1.2.3.4") is True  # window expired, allowed again


def test_login_throttle_tracks_failures_per_client_address_independently() -> None:
    """A shared (non-per-address) counter would let one abusive address
    lock out every other legitimate client."""
    throttle = LoginThrottle(now=lambda: 0.0, sleep=lambda _: None)
    for _ in range(10):
        throttle.record_failure("1.2.3.4")

    assert throttle.allow("1.2.3.4") is False
    assert throttle.allow("5.6.7.8") is True
