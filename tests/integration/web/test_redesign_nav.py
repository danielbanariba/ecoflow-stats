"""Integration tests for redesign01's base layout: the data-driven nav
in `base.html` (visual-QA batch fix01's original defect class: a
hardcoded `<a>` per page) and the favicon link that fixes the previous
404.

Built through the real `create_app`/`bootstrap.build` composition root,
the same way the other `tests/integration/web/*.py` modules do.
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
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_PASSWORD = "a-strong-test-password-2"


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


def _build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, password: str | None = None
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


def _client(application: bootstrap.Application) -> TestClient:
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    return TestClient(app)


def test_the_nav_links_every_page_and_marks_only_the_current_one_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """redesign01's nav is a single data-driven list in `base.html`
    (visual-QA batch fix01's original defect class: a hardcoded <a> per
    page). Pass-1: a nav that always marks the same page active
    regardless of which page actually rendered -- or drops a page from
    the list entirely -- would mislead a user about where they are, or
    make an already-shipped page undiscoverable."""
    application = _build(monkeypatch, tmp_path)
    try:
        with _client(application) as client:
            overview_html = client.get("/").text
            outages_html = client.get("/outages").text

        for href in ('href="/"', 'href="/outages"', 'href="/battery"'):
            assert href in overview_html
            assert href in outages_html

        assert '<a href="/" aria-current="page">' in overview_html
        assert '<a href="/outages" aria-current="page">' not in overview_html

        assert '<a href="/outages" aria-current="page">' in outages_html
        assert '<a href="/" aria-current="page">' not in outages_html
    finally:
        application.database.close()


def test_base_html_links_the_favicon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Scope rule "fixes the current favicon 404". Pass-1: a page with
    no `<link rel="icon">` leaves every browser re-requesting
    `/favicon.ico`, a request the app has never had a route for, on
    every single navigation."""
    application = _build(monkeypatch, tmp_path)
    try:
        with _client(application) as client:
            html = client.get("/").text

        assert '<link rel="icon"' in html
        assert 'href="/static/favicon.svg"' in html
    finally:
        application.database.close()


def test_the_logout_button_appears_only_when_logged_in_with_a_password_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-10 (qa-report-ui-01.md): no logout control existed at all --
    adds one, but only where it actually means something: never on a
    no-password (LAN-guard-only) instance, where there is no session to
    log out of at all; never on the login page before a session
    exists; and gone again the instant the real `/logout` route (its
    own token scraped from the rendered page, not computed
    independently) actually revokes that session."""
    no_password_app = _build(monkeypatch, tmp_path)
    try:
        with _client(no_password_app) as client:
            html = client.get("/").text
        assert 'action="/logout"' not in html
    finally:
        no_password_app.database.close()

    application = _build(monkeypatch, tmp_path, password=_PASSWORD)
    try:
        with _client(application) as client:
            login_html = client.get("/login").text
            assert 'action="/logout"' not in login_html  # not logged in yet

            match = re.search(r'name="csrf_token" value="([^"]+)"', login_html)
            assert match is not None
            client.post(
                "/login", data={"password": _PASSWORD, "next": "/", "csrf_token": match.group(1)}
            )

            home_html = client.get("/").text
            assert 'action="/logout"' in home_html

            logout_match = re.search(
                r'action="/logout">\s*<input type="hidden" name="csrf_token" value="([^"]+)"',
                home_html,
            )
            assert logout_match is not None, "logout form rendered no csrf_token field"
            logout_response = client.post(
                "/logout", data={"csrf_token": logout_match.group(1)}, follow_redirects=False
            )
            assert logout_response.status_code == 303

            after_logout_html = client.get("/login").text
        assert 'action="/logout"' not in after_logout_html
    finally:
        application.database.close()
