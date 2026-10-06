"""Named Defect "Secrets exposed", full contract (design-edges testing
strategy table): with sentinel secrets configured, no page, HTMX
partial, API response, or captured log line contains any of them.

The page/partial/API half is proven through the real
`create_app`/`bootstrap.build` composition root, the same way
`tests/integration/web/test_overview_page.py` already does. The log
half reuses the real `configure_logging`/`RedactionFilter` from
`ecoflow_stats.logs` (its own unit tests, `tests/unit/test_logs_redaction.py`,
already prove the filter's string-replacement logic in isolation; this
test instead proves the filter is actually *wired in* for a record
emitted the way almost every module in this codebase really logs: via
`logging.getLogger(__name__)`, not the root logger directly).

Every `TemplateResponse` call site in `web/routes/pages.py` was audited
for this slice: none of them pass a `Settings` object into template
context today (only `t`, `lang`, `html_lang`, `view`, `next`, `error`,
`csrf_token` — see `apply-progress-batch12`), so no template edit was
needed to satisfy "templates receive only `PublicSettings`, never
`Settings`".
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.logs import configure_logging
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_ACCESS_KEY = "SENTINEL-ACCESS-KEY-0001"
_SECRET_KEY = "SENTINEL-SECRET-KEY-0002"
_PASSWORD = "sentinel-password-0003"
_NTFY_TOPIC = "sentinel-ntfy-topic-0004"
_NTFY_TOKEN = "sentinel-ntfy-token-0005"

_SENTINELS = (_ACCESS_KEY, _SECRET_KEY, _PASSWORD, _NTFY_TOPIC, _NTFY_TOKEN)


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, password: str | None
) -> bootstrap.Application:
    env = {
        "ECOFLOW_ACCESS_KEY": _ACCESS_KEY,
        "ECOFLOW_SECRET_KEY": _SECRET_KEY,
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
        "ECOFLOW_STATS_NTFY_TOPIC": _NTFY_TOPIC,
        "ECOFLOW_STATS_NTFY_TOKEN": _NTFY_TOKEN,
    }
    if password is not None:
        env["ECOFLOW_STATS_PASSWORD"] = password
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    return bootstrap.build(settings, clock=FakeClock(_NOW))


def _assert_no_sentinel(text: str, *, where: str) -> None:
    leaked = [sentinel for sentinel in _SENTINELS if sentinel in text]
    assert leaked == [], f"sentinel secret(s) {leaked} leaked into {where}"


def test_no_page_partial_or_api_response_contains_a_configured_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No password configured: every route is reachable without a
    session, so this exercises the overview page, its live partial, and
    the JSON status API with real rendered/serialized content."""
    application = _build(monkeypatch, tmp_path, password=None)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app) as client:
            responses = {
                "GET /": client.get("/"),
                "GET /healthz": client.get("/healthz"),
                "GET /partials/live": client.get("/partials/live"),
                "GET /api/v1/status": client.get("/api/v1/status"),
            }

        for where, response in responses.items():
            assert response.status_code == 200, where
            _assert_no_sentinel(response.text, where=where)
    finally:
        application.database.close()


def test_the_login_page_never_shows_the_configured_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Password configured: the one page rendered *before* any session
    exists must still never echo the password back, including on a
    failed-attempt redisplay."""
    application = _build(monkeypatch, tmp_path, password=_PASSWORD)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app) as client:
            login_page = client.get("/login")
            failed_attempt = client.get("/login", params={"error": "1"})

        _assert_no_sentinel(login_page.text, where="GET /login")
        _assert_no_sentinel(failed_attempt.text, where="GET /login?error=1")
    finally:
        application.database.close()


def test_an_authenticated_overview_page_never_shows_the_configured_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Password configured, session established: the overview page a
    logged-in user actually sees must not leak the password either --
    `PublicSettings` has no `password` field at all, so this would only
    regress if a future page started handing a template the full
    `Settings` object instead."""
    application = _build(monkeypatch, tmp_path, password=_PASSWORD)
    try:
        app = create_app(application, start_collector=_never_ticks, start_derive_job=_never_ticks)

        with TestClient(app) as client:
            client.post("/login", data={"password": _PASSWORD, "next": "/"})
            home = client.get("/")

        assert home.status_code == 200
        _assert_no_sentinel(home.text, where="GET / (authenticated)")
    finally:
        application.database.close()


def test_a_log_line_from_any_module_never_shows_a_configured_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The logging half of the contract, exercised the way the real
    composition root wires it (`cli.py` calls `configure_logging` before
    serving). Almost every module in this codebase logs through its own
    `logging.getLogger(__name__)`, never the root logger directly, so
    the probe below uses a representative non-root logger name -- the
    shape every real log statement in `acquisition/`, `web/`, etc.
    actually has."""
    application = _build(monkeypatch, tmp_path, password=_PASSWORD)
    try:
        settings = application.settings
        configure_logging(settings)

        with caplog.at_level(logging.INFO):
            logging.getLogger("ecoflow_stats.acquisition.ecoflow_client").info(
                "signing request with access_key=%s secret_key=%s", _ACCESS_KEY, _SECRET_KEY
            )
            logging.getLogger("ecoflow_stats.notifications.service").info(
                "posting to ntfy topic=%s token=%s", _NTFY_TOPIC, _NTFY_TOKEN
            )

        _assert_no_sentinel(caplog.text, where="captured log output")
    finally:
        application.database.close()
