"""Integration tests for API2-01 (qa-report-data-02.md): every page
route and HTMX review-action route must reject an out-of-range
`from`/`to`/path/form timestamp with a clean 4xx, never the unhandled
`OverflowError` -> bare `500` round 1's API-01 fix left reachable
through a bigger surface -- `web.routes.pages` and `web.routes.actions`
each built their own unchecked `range_start`/`range_end` (or passed an
unchecked path/form int straight into a store call) instead of sharing
API-01's own bound check.

Built through the real `create_app`/`bootstrap.build` composition
root, the same harness `test_outages_page.py`/`test_review_actions.py`
already use, so every fix is proven wired end-to-end -- including the
security-header/no-stack-trace guarantee (SEC-05/SEC-06) on the new
400 responses themselves, not just a bare status-code check.
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
from ecoflow_stats.outages.model import Gap
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.security import CSRF_COOKIE, csrf_token
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_HUGE = 99_999_999_999_999_999_999_999  # exceeds both SQLite int64 and datetime's range


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


def _gap(start_ts: int, end_ts: int | None, **overrides: object) -> Gap:
    defaults: dict[str, object] = {
        "cause": "unknown",
        "state_before": "present",
        "state_after": "present",
        "soc_before": 80,
        "soc_after": 75,
        "chg_ac_wh_delta": None,
        "expected_in_wh": None,
        "evidence": "inconclusive",
        "failures": {},
    }
    defaults.update(overrides)
    return Gap(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def _client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, gaps: list[Gap] | None = None
) -> tuple[bootstrap.Application, int, TestClient]:
    application = _build(monkeypatch, tmp_path)
    (device,) = application.device_records
    outage_store = OutageStore(application.database.writer)
    outage_store.replace_from(device.id, None, [], gaps or [])
    application.database.writer.commit()
    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    return application, device.id, TestClient(app)


def _csrf_headers(client: TestClient, application: bootstrap.Application) -> dict[str, str]:
    client.get("/")
    return {"x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])}


@pytest.mark.parametrize("path", ["/outages", "/battery", "/energy", "/grid"])
def test_a_page_route_rejects_an_astronomically_large_to_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    """Pass-1 (API2-01): the orchestrator's own repro -- e.g.
    `/grid?from=1&to=99999999999999999999` -- crashed every main
    navigation page with an unhandled `OverflowError` -> bare `500`
    before this fix, because each page route built its own
    `range_start`/`range_end` inline with no bound check at all. A
    huge/out-of-range `to` is invalid client input and must become a
    clean `400`, the same outcome API-01 already gave the JSON API,
    never a crash -- and the themed error page must still carry the
    full security-header set (SEC-05/SEC-06) with no stack trace."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get(path, params={"device": device_id, "from": 1, "to": _HUGE})

        assert response.status_code == 400
        assert "Traceback" not in response.text
        assert "OverflowError" not in response.text
        assert response.headers.get("content-security-policy")
        assert response.headers.get("x-frame-options") == "DENY"
        assert response.headers.get("cache-control") == "no-store"
    finally:
        application.database.close()


@pytest.mark.parametrize("path", ["/outages/gaps", "/outages/legacy"])
def test_a_review_list_fragment_rejects_an_astronomically_large_to_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    """Pass-1 (API2-01): the gap/legacy review list's own `GET`
    fragment routes built the exact same unchecked range the pages
    above did -- a bookmarked/shared `hx-get` URL with an over-large
    `to` crashed them too, never surfacing a feedback fragment."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            response = client.get(path, params={"device": device_id, "from": 1, "to": _HUGE})

        assert response.status_code == 400
        assert "Traceback" not in response.text
    finally:
        application.database.close()


def test_deciding_a_gap_with_a_huge_gap_start_path_param_is_rejected_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (API2-01): the QA report's repro for `decide_gap` --
    `POST /outages/gaps/<huge>/decision` -- crashed with `OverflowError:
    Python int too large to convert to SQLite INTEGER` inside
    `OutageStore.gaps` (via `_find_gap`), reached because `gap_start`
    was never bound-checked before this fix."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            headers = _csrf_headers(client, application)
            response = client.post(
                f"/outages/gaps/{_HUGE}/decision",
                data={"device": device_id, "verdict": "outage"},
                headers=headers,
            )

        assert response.status_code == 400
        assert "Traceback" not in response.text
    finally:
        application.database.close()


def test_deciding_a_legacy_entry_with_a_huge_start_path_param_is_rejected_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (API2-01): `decide_legacy`'s own `start` path param
    reaches `LegacyStore.get`'s raw SQLite bind the same unchecked way
    `decide_gap`'s `gap_start` does."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            headers = _csrf_headers(client, application)
            response = client.post(
                f"/outages/legacy/{_HUGE}/decision",
                data={"device": device_id, "verdict": "real"},
                headers=headers,
            )

        assert response.status_code == 400
        assert "Traceback" not in response.text
    finally:
        application.database.close()


def test_undo_with_a_huge_decision_id_path_param_is_rejected_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (API2-01): `undo`'s own `decision_id` path param is
    handed straight to `DecisionStore.undo`'s raw SQLite bind before
    this fix -- any Python int outside SQLite's signed-64-bit range
    raises `OverflowError` at bind time regardless of the target
    column's declared type."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            headers = _csrf_headers(client, application)
            response = client.post(
                f"/decisions/{_HUGE}/undo",
                data={"device": device_id, "target": "gap", "start": 1},
                headers=headers,
            )

        assert response.status_code == 400
        assert "Traceback" not in response.text
    finally:
        application.database.close()


def test_undo_with_a_huge_start_form_field_is_rejected_with_400_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (API2-01): the QA report's second `undo` repro -- a huge
    `start` FORM field, not just a huge path `decision_id` -- reaches
    `_find_gap`'s own unchecked `OutageStore.gaps` bind the same way."""
    application, device_id, client = _client(monkeypatch, tmp_path)
    try:
        with client:
            headers = _csrf_headers(client, application)
            response = client.post(
                "/decisions/1/undo",
                data={"device": device_id, "target": "gap", "start": _HUGE},
                headers=headers,
            )

        assert response.status_code == 400
        assert "Traceback" not in response.text
    finally:
        application.database.close()
