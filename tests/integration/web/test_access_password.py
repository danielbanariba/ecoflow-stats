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
    CSRF_COOKIE,
    LoginThrottle,
    csrf_token,
    issue_session_token,
    session_signing_key,
    verify_session_token,
)
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_PASSWORD = "correct-password-1"
_LOCAL_CLIENT = ("127.0.0.1", 12345)


def _csrf_headers(client: TestClient, application: bootstrap.Application) -> dict[str, str]:
    """SEC-01 made `require_csrf` fail closed: every `/login` POST in
    this file now needs real CSRF proof, exactly like the rendered
    `login.html` page provides -- a GET first mints the real `efs_csrf`
    cookie, and the header is computed from that exact value."""
    client.get("/login")
    return {"x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])}


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
            login_response = client.post(
                "/login",
                data={"password": _PASSWORD, "next": "/"},
                headers=_csrf_headers(client, application),
            )
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
            login_response = client.post(
                "/login",
                data={"password": "totally-wrong", "next": "/"},
                headers=_csrf_headers(client, application),
            )
            response = client.get("/")

        assert login_response.status_code == 303  # rejected by password check, not CSRF
        assert response.status_code == 303  # still no session
    finally:
        application.database.close()


def test_a_wrong_password_and_a_throttled_lockout_show_different_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-17 (qa-report-ui-01.md): the report's exact finding -- a wrong
    password and a throttled lockout rendered the identical message,
    giving a locked-out visitor no idea they are not simply mistyping
    their password. Proves the two are not just individually present
    somewhere, but genuinely distinct: the wrong-password page must
    never also contain the throttled page's own text, and vice versa."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=True) as client:
            headers = _csrf_headers(client, application)
            wrong_password_response = client.post(
                "/login", data={"password": "totally-wrong", "next": "/"}, headers=headers
            )
            for _ in range(10):
                client.post(
                    "/login", data={"password": "totally-wrong", "next": "/"}, headers=headers
                )
            throttled_response = client.post(
                "/login", data={"password": "totally-wrong", "next": "/"}, headers=headers
            )

        assert "Incorrect password" in wrong_password_response.text
        assert "Too many failed attempts" not in wrong_password_response.text

        assert "Too many failed attempts" in throttled_response.text
        assert "Incorrect password" not in throttled_response.text
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
            response = client.post(
                "/login",
                data={"password": _PASSWORD, "next": "/"},
                headers=_csrf_headers(client, application),
            )

        assert response.status_code == 303  # a real session was actually granted
        set_cookie = response.headers.get("set-cookie", "").lower()
        assert "efs_session=" in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
    finally:
        application.database.close()


def test_a_wrong_password_attempt_is_not_delayed_by_the_real_login_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEC-04 (qa-report-data-01.md): the real production route (not
    just the pure `LoginThrottle` class below) must no longer block the
    worker thread it runs on for a whole second per failed attempt --
    that blocking `time.sleep` was the thread-pool-exhaustion risk the
    QA report flagged. A single failed attempt now returns essentially
    immediately."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT) as client:
            headers = _csrf_headers(client, application)
            started = time.monotonic()
            client.post("/login", data={"password": "totally-wrong", "next": "/"}, headers=headers)
            elapsed = time.monotonic() - started

        assert elapsed < 0.5
    finally:
        application.database.close()


def test_the_real_login_route_rejects_with_429_and_retry_after_once_throttled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEC-04: once a client address is over the 10-failures-per-5-
    minutes budget, the real production route must reject with
    `429 Retry-After` instead of ever attempting to verify the
    password again -- proves the route is actually wired to
    `LoginThrottle.allow`/`retry_after`, not just that the pure class
    computes the right numbers in isolation."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            headers = _csrf_headers(client, application)
            for _ in range(10):
                client.post(
                    "/login", data={"password": "totally-wrong", "next": "/"}, headers=headers
                )
            eleventh = client.post(
                "/login", data={"password": "totally-wrong", "next": "/"}, headers=headers
            )

        assert eleventh.status_code == 429
        retry_after = int(eleventh.headers["retry-after"])
        assert 0 < retry_after <= 301  # the full 300s window, plus the route's rounding-up second
        assert "Too many failed attempts" in eleventh.text
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


def test_logging_out_revokes_every_previously_issued_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEC-03 (qa-report-data-01.md): a signed session cookie must stop
    working the moment `/logout` runs, not only after the next process
    restart (the only revocation that existed before this fix, since
    `app_secret` is fresh random bytes every `bootstrap.build` call).
    Simulates the exact risk the report names: a copy of the cookie
    taken before logout -- a stolen cookie, or the same browser's own
    back button -- must be rejected afterward, not merely the
    browser's own now-cleared cookie jar."""
    application = _build(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )

        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            client.post(
                "/login",
                data={"password": _PASSWORD, "next": "/"},
                headers=_csrf_headers(client, application),
            )
            stolen_session_cookie = client.cookies["efs_session"]

            client.post("/logout", headers=_csrf_headers(client, application))

            client.cookies.set("efs_session", stolen_session_cookie)
            replay_response = client.get("/")

        assert replay_response.status_code == 303  # the stolen cookie no longer works
    finally:
        application.database.close()


def test_login_throttle_caps_at_ten_failures_in_five_minutes_without_sleeping() -> None:
    """Design, "Password set": "at most 10 failures per 5 minutes per
    client address". SEC-04: `record_failure` must never sleep -- there
    is no `sleep` parameter to inject at all any more, so a
    reintroduced blocking call would be a `TypeError` here, not a
    silent regression."""
    clock = {"t": 0.0}
    throttle = LoginThrottle(now=lambda: clock["t"])

    for _ in range(10):
        assert throttle.allow("1.2.3.4") is True
        throttle.record_failure("1.2.3.4")

    assert throttle.allow("1.2.3.4") is False  # the 11th attempt is capped
    assert throttle.retry_after("1.2.3.4") == pytest.approx(300.0)

    clock["t"] += 301  # advance past the 5-minute window
    assert throttle.allow("1.2.3.4") is True  # window expired, allowed again
    assert throttle.retry_after("1.2.3.4") == 0.0


def test_login_throttle_tracks_failures_per_client_address_independently() -> None:
    """A shared (non-per-address) counter would let one abusive address
    lock out every other legitimate client."""
    throttle = LoginThrottle(now=lambda: 0.0)
    for _ in range(10):
        throttle.record_failure("1.2.3.4")

    assert throttle.allow("1.2.3.4") is False
    assert throttle.allow("5.6.7.8") is True
