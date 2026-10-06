"""Integration tests for `/api/v1/*` input-validation and auth-response
robustness (QA report `qa-report-data-01.md`, findings API-01 and API-03).

API-01: an out-of-range, inverted, or non-numeric `from`/`to` must be
rejected with a clean `422` JSON error -- never the unhandled
`OverflowError` -> bare `500` the QA report reproduced on
`battery/series`, `grid/daily`, and `energy/daily`. The fix lives in one
shared place (`web.routes.api._resolve_range`), so this file exercises a
handful of representative routes rather than every one, mirroring
`test_api_grid.py`'s lightweight router-only harness.

API-03: an unauthenticated `/api/v1/*` request on a password-protected
instance must get a `401` JSON body, not the browser-oriented `303` HTML
redirect every page route still correctly gets -- built through the real
`create_app`/`bootstrap.build` composition root, the same way
`test_access_password.py` already does, since the behavior lives in
`AccessControlMiddleware`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats import bootstrap
from ecoflow_stats.config import load_settings
from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.web.app import create_app
from ecoflow_stats.web.routes.api import ApiContext, router
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_HUGE = 99_999_999_999_999_999_999_999  # exceeds both SQLite int64 and datetime's range


def _reading(**overrides: object) -> Reading:
    return Reading(**overrides)  # type: ignore[arg-type]


def _client(tmp_path: Path) -> tuple[TestClient, Database, int]:
    """Lightweight router-only harness, mirroring `test_api_grid.py`:
    real SQLite stores, no real composition root, no auth."""
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    sample_store = SampleStore(db.writer)
    sample_store.add(record.id, _NOW_TS - 60, 1, _reading(soc=80.0))
    db.writer.commit()

    app = FastAPI()
    app.state.api = ApiContext(
        now=lambda: _NOW,
        stale_threshold_s=180,
        poll_interval_s=60,
        detector_config=DetectorConfig(),
        sample_store=sample_store,
        outage_store=None,  # type: ignore[arg-type]
        device_records=(record,),
        tz="UTC",
    )
    app.state.application = SimpleNamespace(database=db)
    app.include_router(router)
    return TestClient(app), db, record.id


def test_battery_series_rejects_an_astronomically_large_to_with_422(tmp_path: Path) -> None:
    """Pass-1 (API-01): the orchestrator's own repro --
    `/api/v1/battery/series?from=...&to=99999999999999999999` -- crashed
    with an unhandled `OverflowError` -> bare `500` before this fix,
    because `SampleStore.between()` binds the raw value straight into a
    parameterized SQLite query whose INTEGER column has a signed 64-bit
    max. A client's out-of-range input must become a `422`, not a
    `500`."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get(
            "/api/v1/battery/series", params={"device": device_id, "from": 0, "to": _HUGE}
        )
        assert response.status_code == 422
        assert "detail" in response.json()
    finally:
        db.close()


def test_grid_daily_rejects_an_astronomically_large_from_with_422(tmp_path: Path) -> None:
    """Pass-1 (API-01): the QA report's second repro
    (`grid/daily?from=99999999999999&to=99999999999999999`) crashed the
    same way via `timeutil.local_day()`'s `datetime.fromtimestamp()`,
    which raises `OverflowError` for a platform-unrepresentable
    timestamp -- a different crash site than `battery/series`, proving
    the fix must sit ahead of every call site, not patch one function."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get(
            "/api/v1/grid/daily", params={"device": device_id, "from": _HUGE, "to": _HUGE}
        )
        assert response.status_code == 422
    finally:
        db.close()


def test_energy_daily_rejects_a_large_negative_from_with_422(tmp_path: Path) -> None:
    """Pass-1 (API-01): the QA report's third repro used a huge
    *negative* `from` (`energy/daily?from=-999...&to=...`) -- the same
    crash class in the opposite direction, so the bound must reject both
    tails, not just an overly large positive value."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get(
            "/api/v1/energy/daily", params={"device": device_id, "from": -_HUGE, "to": _NOW_TS}
        )
        assert response.status_code == 422
    finally:
        db.close()


def test_a_from_after_to_is_rejected_with_422_instead_of_an_empty_200(tmp_path: Path) -> None:
    """Pass-1 (API-01): the task requires `from > to` to be rejected,
    not silently accepted as an (empty) valid range -- a client that
    swapped its own params by mistake should see a clear error, not a
    confusingly empty-but-200 response."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get(
            "/api/v1/battery/series",
            params={"device": device_id, "from": _NOW_TS, "to": _NOW_TS - 1000},
        )
        assert response.status_code == 422
    finally:
        db.close()


def test_a_non_numeric_from_is_rejected_with_422_not_a_500(tmp_path: Path) -> None:
    """Pass-1 (API-01): the task also requires a non-numeric `from`/`to`
    to return a clean `422`. FastAPI's own `int | None` query-param
    typing already converts a non-numeric value into a `RequestValidationError`
    (-> 422) before the route body ever runs; this test locks that
    contract so a future change to a looser param type (e.g. `str`)
    would be caught."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get(
            "/api/v1/battery/series", params={"device": device_id, "from": "not-a-number"}
        )
        assert response.status_code == 422
    finally:
        db.close()


# --- API-03: unauthenticated /api/v1/* gets 401 JSON, pages still 303 ------

_PASSWORD = "correct-password-1"
_LOCAL_CLIENT = ("127.0.0.1", 12345)


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _build_protected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> bootstrap.Application:
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
    import asyncio

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-collector", target=_blocks_forever, clock=application.clock
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


def test_an_unauthenticated_api_v1_request_gets_401_json_not_a_303_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1 (API-03): the QA repro -- `GET /api/v1/battery/series`
    with no session on a password-protected instance -- returned an
    empty-bodied `303` pointing at `/login`, which confuses any
    non-browser API client. It must return `401` with a JSON body
    instead."""
    application = _build_protected(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )
        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            response = client.get("/api/v1/battery/series")

        assert response.status_code == 401
        assert response.json()["detail"]
    finally:
        application.database.close()


def test_an_unauthenticated_page_request_still_gets_a_303_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: fixing API-03 must not break the browser page
    flow -- an unauthenticated page request must keep redirecting to
    `/login`, exactly as before."""
    application = _build_protected(monkeypatch, tmp_path)
    try:
        app = create_app(
            application,
            start_collector=_never_ticks,
            start_derive_job=_never_ticks,
            start_rollups_job=_never_ticks,
        )
        with TestClient(app, client=_LOCAL_CLIENT, follow_redirects=False) as client:
            response = client.get("/outages")

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")
    finally:
        application.database.close()
