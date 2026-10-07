"""Integration tests for `GET /healthz` (amendment A1; design D13).

A standalone `FastAPI()` with only the health router mounted, and
`app.state.health` set directly to a hand-built `HealthContext` — no real
collector, no real database. That keeps this fast and deterministic;
`tests/integration/web/test_app.py` separately proves `create_app` wires
a real `HealthContext` from real pieces via the lifespan.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats.web.routes.health import HealthContext, router


def _client(
    *,
    stale_threshold_s: int = 180,
    collector_alive: bool = True,
    collector_running: bool = True,
    db_writable: bool = True,
    device_ages: dict[int, int | None] | None = None,
    now_s: int = 1_000_000,
) -> TestClient:
    ages = device_ages if device_ages is not None else {1: 0}
    app = FastAPI()
    app.state.health = HealthContext(
        now_s=lambda: now_s,
        stale_threshold_s=stale_threshold_s,
        collector_alive=lambda: collector_alive,
        collector_running=lambda: collector_running,
        db_writable=lambda: db_writable,
        device_ids=tuple(ages),
        last_sample_ts=lambda device_id: (
            None if ages[device_id] is None else now_s - ages[device_id]
        ),
    )
    app.include_router(router)
    return TestClient(app)


def test_healthz_reports_200_ok_for_a_fresh_sample() -> None:
    client = _client(device_ages={1: 5})

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["devices"] == [{"id": 1, "last_sample_age_s": 5}]


def test_healthz_reports_200_degraded_for_a_stale_sample() -> None:
    client = _client(stale_threshold_s=180, device_ages={1: 181})

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_healthz_reports_200_starting_when_a_device_has_no_sample_yet() -> None:
    client = _client(device_ages={1: None})

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "starting"
    assert body["devices"] == [{"id": 1, "last_sample_age_s": None}]


def test_healthz_reports_503_when_the_collector_task_is_dead() -> None:
    """Amendment A1: dead collector -> 503, regardless of how fresh the
    last sample still happens to be."""
    client = _client(collector_alive=False, device_ages={1: 0})

    response = client.get("/healthz")

    assert response.status_code == 503


def test_healthz_reports_503_when_the_database_is_unwritable() -> None:
    client = _client(db_writable=False, device_ages={1: 0})

    response = client.get("/healthz")

    assert response.status_code == 503


def test_healthz_reports_restarting_only_while_200_and_running_is_false() -> None:
    """The `collector` body field surfaces an ordinary crash backoff
    (`running=False` but still `alive`) distinctly from "ok" — this is
    the 200 case only; a dead collector is 503, tested separately above."""
    client = _client(collector_alive=True, collector_running=False, device_ages={1: 0})

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["collector"] == "restarting"
