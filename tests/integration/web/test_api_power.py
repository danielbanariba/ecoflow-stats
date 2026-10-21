"""Integration tests for the AC-load read-only API route: `GET
/api/v1/power/series`.

A standalone `FastAPI()` with only the api router mounted and
`app.state.api` set directly to a hand-built `ApiContext` against real
SQLite stores -- the same harness `test_api_grid.py` uses.
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
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.web.routes.api import ApiContext, router

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


def _client(
    tmp_path: Path, *, samples: list[tuple[int, float | None]] | None = None
) -> tuple[TestClient, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    sample_store = SampleStore(db.writer)
    for ts, ac_out_w in samples or []:
        sample_store.add(record.id, ts, 1, Reading(ac_out_w=ac_out_w))
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
    app.include_router(router)
    return TestClient(app), record.id


def test_power_series_buckets_at_1_minute_for_a_range_of_24_hours_or_less(tmp_path: Path) -> None:
    """Acceptance criterion "ranges up to 24 h use 1-minute buckets",
    proven through the route at its upper edge. Pass-2: wiring the route
    to `grid/series`'s own 5-minute tier table, or comparing the day
    span with `<` instead of `<=`, turns this red."""
    client, _device_id = _client(tmp_path)

    response = client.get("/api/v1/power/series", params={"from": _NOW_TS - 86_400, "to": _NOW_TS})

    body = response.json()
    assert body["bucket_width_s"] == 60
    assert len(body["points"]) == 1_440


def test_power_series_moves_to_coarser_buckets_just_beyond_24_hours(tmp_path: Path) -> None:
    """Pass-1: catches the 1-minute tier being applied to every range,
    which would return 10,000+ points for a week. Pass-2: making the
    1-minute tier open-ended (`(None, 60)`) turns this red."""
    client, _device_id = _client(tmp_path)

    response = client.get(
        "/api/v1/power/series", params={"from": _NOW_TS - 86_400 - 1, "to": _NOW_TS}
    )

    assert response.json()["bucket_width_s"] > 60


def test_power_series_returns_the_ac_load_per_bucket_with_explicit_null_gaps(
    tmp_path: Path,
) -> None:
    """Pass-1: catches the route reading the wrong `Reading` field,
    mis-mapping the helper's points into the JSON body, or skipping an
    empty bucket (no explicit `null` for the chart to break on). Pass-2:
    serializing `ac_in_w` instead of `ac_out_w`, or filtering out the
    helper's null points, turns this red."""
    range_start = _NOW_TS - 240
    samples: list[tuple[int, float | None]] = [
        (range_start, 200.0),
        (range_start + 30, 400.0),  # same minute: avg 300, max 400
        (range_start + 60, None),  # reported nothing
        # minute 2 has no sample at all
        (range_start + 180, 50.0),
    ]
    client, device_id = _client(tmp_path, samples=samples)

    response = client.get(
        "/api/v1/power/series", params={"from": range_start, "to": range_start + 240}
    )

    body = response.json()
    assert body["schema"] == "ecoflow-stats.power-series/v1"
    assert body["device_id"] == device_id
    assert body["range"] == {"start": range_start, "end": range_start + 240}
    assert [
        (point["ts"] - range_start, point["ac_out_w_avg"], point["ac_out_w_max"])
        for point in body["points"]
    ] == [(0, 300.0, 400.0), (60, None, None), (120, None, None), (180, 50.0, 50.0)]


def test_power_series_returns_404_for_an_unknown_device(tmp_path: Path) -> None:
    """A typo'd or stale `device` must not silently serve the first
    configured device's load under another id (API-02, the same rule
    `grid/series` follows). Pass-2: skipping `_resolve_device_id` in the
    route turns this red with a 200."""
    client, device_id = _client(tmp_path)

    response = client.get("/api/v1/power/series", params={"device": device_id + 99})

    assert response.status_code == 404
