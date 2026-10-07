"""Integration tests for `GET /api/v1/status` (live-status requirement:
"Versioned Read-Only Status Endpoint").

A standalone `FastAPI()` with only the api router mounted and
`app.state.api` set directly to a hand-built `ApiContext` against real
SQLite stores — no real collector, no real composition root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.web.routes.api import ApiContext, router

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


def _client(tmp_path: Path) -> tuple[TestClient, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    sample_store = SampleStore(db.writer)
    sample_store.add(record.id, _NOW_TS - 10, 1, Reading(grid_v=120.0, soc=81))
    outage_store = OutageStore(db.writer)

    app = FastAPI()
    app.state.api = ApiContext(
        now=lambda: _NOW,
        stale_threshold_s=180,
        poll_interval_s=60,
        detector_config=DetectorConfig(),
        sample_store=sample_store,
        outage_store=outage_store,
        device_records=(record,),
    )
    app.include_router(router)
    return TestClient(app), db, record.id


def test_the_endpoint_returns_the_versioned_read_only_contract(tmp_path: Path) -> None:
    """Pass-1: a wrong schema string or a missing top-level field would
    break every future consumer (the panel follow-up) this versioned
    contract exists to protect (scenario "The endpoint returns a
    read-only JSON contract")."""
    client, db, device_id = _client(tmp_path)
    try:
        response = client.get("/api/v1/status")

        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == "ecoflow-stats.status/v1"
        assert body["generated_at"] == _NOW_TS
        assert len(body["devices"]) == 1
        device = body["devices"][0]
        assert device["id"] == device_id
        assert device["age_s"] == 10
        assert device["stale"] is False
        assert device["grid"] == "present"
        assert device["outage"] == {"ongoing": False, "since": None}
        assert device["reading"]["soc"] == 81
    finally:
        db.close()


def test_a_second_request_does_not_change_the_stored_data(tmp_path: Path) -> None:
    """Pass-1: a status endpoint that mutated anything (even
    accidentally, e.g. by writing a cache row) would violate "no request
    capable of mutating device or application state"."""
    client, db, device_id = _client(tmp_path)
    try:
        before = client.get("/api/v1/status").json()
        after = client.get("/api/v1/status").json()

        assert before == after
        samples = list(SampleStore(db.writer).between(device_id, 0, _NOW_TS))
        assert len(samples) == 1  # still exactly the one seeded sample
    finally:
        db.close()


def test_existing_fields_survive_a_future_versions_additions(tmp_path: Path) -> None:
    """Scenario "Existing fields survive a future version's additions":
    this test only ever asserts individual known keys are present with
    their documented meaning, never the full set of keys -- so adding a
    new field to the contract later cannot break it, exactly the
    property the requirement asks for."""
    client, db, _device_id = _client(tmp_path)
    try:
        body = client.get("/api/v1/status").json()
        device = body["devices"][0]

        for key in ("id", "ts", "age_s", "stale", "grid", "outage", "reading"):
            assert key in device
    finally:
        db.close()
