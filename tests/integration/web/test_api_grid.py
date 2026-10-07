"""Integration tests for the 2 grid-quality read-only API routes (task
20.3/20.4's API half): `GET /api/v1/grid/series`, `GET /api/v1/grid/daily`.

A standalone `FastAPI()` with only the api router mounted and
`app.state.api` set directly to a hand-built `ApiContext` against real
SQLite stores -- mirrors `test_api_outages.py`'s own harness. A minimal
stand-in object on `app.state.application` supplies the cross-context
`RollupStore` read `web.routes.api._rollup_store` already uses for
`battery/trends` (no new `ApiContext` field needed for a `RollupStore`).

One test (`test_the_real_derive_path_persists_grid_ranges_the_api_then_
serves_them`) instead builds the real `create_app`/`bootstrap.build`
composition root and drives the actual `derive_rollups` function, so
this batch's new grid computation is proven reachable end to end --
not only unit-tested in isolation (the "zero production callers" gap
class this project has hit twice before).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.rollups import RollupStore
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.web.routes.api import ApiContext, router

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_DEFAULT_DETECTOR_CONFIG = DetectorConfig()


def _reading(**overrides: object) -> Reading:
    return Reading(**overrides)  # type: ignore[arg-type]


def _client(
    tmp_path: Path,
    *,
    samples: list[tuple[int, Reading]] | None = None,
    detector_config: DetectorConfig = _DEFAULT_DETECTOR_CONFIG,
) -> tuple[TestClient, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    sample_store = SampleStore(db.writer)
    for ts, reading in samples or []:
        sample_store.add(record.id, ts, 1, reading)
    db.writer.commit()

    app = FastAPI()
    app.state.api = ApiContext(
        now=lambda: _NOW,
        stale_threshold_s=180,
        poll_interval_s=60,
        detector_config=detector_config,
        sample_store=sample_store,
        outage_store=None,  # type: ignore[arg-type]
        device_records=(record,),
        tz="UTC",
    )
    app.state.application = SimpleNamespace(database=db)
    app.include_router(router)
    return TestClient(app), db, record.id


# --- grid/daily --------------------------------------------------------


def test_grid_daily_returns_the_persisted_min_avg_max_rows(tmp_path: Path) -> None:
    """Scenario "A normal day reports a voltage and frequency range",
    proven through the route. Pass-1: catches the route reading the
    wrong columns, or mis-mapping a stored row into the JSON body."""
    client, db, device_id = _client(tmp_path)
    RollupStore(db.writer).upsert_grid(
        device_id,
        "2026-01-09",
        grid_v_min=118.0,
        grid_v_avg=120.0,
        grid_v_max=122.0,
        grid_hz_min=59.8,
        grid_hz_avg=60.0,
        grid_hz_max=60.2,
        grid_readings=3,
    )
    db.writer.commit()

    response = client.get(
        "/api/v1/grid/daily",
        params={"from": _NOW_TS - 2 * 86_400, "to": _NOW_TS},
    )

    assert response.status_code == 200
    body = response.json()
    day = next(row for row in body["days"] if row["day"] == "2026-01-09")
    assert (day["grid_v_min"], day["grid_v_avg"], day["grid_v_max"]) == (118.0, 120.0, 122.0)
    assert day["readings"] == 3


def test_grid_daily_reports_a_day_with_no_grid_present_samples_as_null_not_zero(
    tmp_path: Path,
) -> None:
    """Scenario "A day with no grid-present samples reports no range".
    Pass-1: catches the route (or its JSON serialization) collapsing a
    stored NULL into a fabricated `0`, which a chart could mistake for
    "zero volts" rather than "no data"."""
    client, db, device_id = _client(tmp_path)
    RollupStore(db.writer).upsert_grid(
        device_id,
        "2026-01-09",
        grid_v_min=None,
        grid_v_avg=None,
        grid_v_max=None,
        grid_hz_min=None,
        grid_hz_avg=None,
        grid_hz_max=None,
        grid_readings=0,
    )
    db.writer.commit()

    response = client.get(
        "/api/v1/grid/daily",
        params={"from": _NOW_TS - 2 * 86_400, "to": _NOW_TS},
    )

    body = response.json()
    day = next(row for row in body["days"] if row["day"] == "2026-01-09")
    assert day["grid_v_min"] is None
    assert day["readings"] == 0


# --- grid/series: bucketing boundaries (task 20.3/20.5) ----------------


def test_grid_series_buckets_at_5_minutes_for_a_range_of_7_days_or_less(tmp_path: Path) -> None:
    """Task 20.3's bucketing rule, lower boundary. Pass-1: catches an
    off-by-one in the day-span comparison that would pick the wrong
    tier exactly at the 7-day edge."""
    client, _db, _device_id = _client(tmp_path)

    response = client.get(
        "/api/v1/grid/series", params={"from": _NOW_TS - 7 * 86_400, "to": _NOW_TS}
    )

    assert response.json()["bucket_width_s"] == 300


def test_grid_series_buckets_at_1_hour_just_beyond_7_days(tmp_path: Path) -> None:
    client, _db, _device_id = _client(tmp_path)

    response = client.get(
        "/api/v1/grid/series", params={"from": _NOW_TS - 7 * 86_400 - 1, "to": _NOW_TS}
    )

    assert response.json()["bucket_width_s"] == 3_600


def test_grid_series_buckets_at_1_day_beyond_90_days(tmp_path: Path) -> None:
    client, _db, _device_id = _client(tmp_path)

    response = client.get(
        "/api/v1/grid/series", params={"from": _NOW_TS - 91 * 86_400, "to": _NOW_TS}
    )

    assert response.json()["bucket_width_s"] == 86_400


def test_grid_series_returns_one_bucketed_point_per_window_with_its_own_range(
    tmp_path: Path,
) -> None:
    """Pass-1: catches the bucketing grouping all samples into one
    bucket, or computing the range across the wrong window's samples."""
    range_start = _NOW_TS - 600
    samples = [
        (range_start, _reading(grid_v=118.0, grid_hz=59.9)),
        (range_start + 60, _reading(grid_v=122.0, grid_hz=60.1)),
        (range_start + 300, _reading(grid_v=200.0, grid_hz=60.5)),  # second 5-min bucket
    ]
    client, _db, _device_id = _client(tmp_path, samples=samples)

    response = client.get(
        "/api/v1/grid/series", params={"from": range_start, "to": range_start + 600}
    )

    body = response.json()
    assert body["bucket_width_s"] == 300
    assert len(body["points"]) == 2
    first, second = body["points"]
    assert (first["grid_v_min"], first["grid_v_max"]) == (118.0, 122.0)
    assert second["grid_v_min"] == 200.0


def test_grid_series_judges_presence_against_the_configured_threshold_not_the_default(
    tmp_path: Path,
) -> None:
    """DATA-01 (orchestrator finding, qa-report-data-01.md): before
    this fix, `grid_series_route` called `grid_quality_range` with no
    `config=` at all, so it always judged presence against the
    hardcoded 50.0V default no matter what `ApiContext.detector_config`
    actually held -- the same chart could show grid "present" at a
    voltage the outages page (which already reads the real configured
    threshold) judges absent.

    Pass-2 target: dropping the route's new `config=ctx.detector_config`
    argument turns this red -- a lone 60V sample would be judged
    PRESENT under the hardcoded 50.0V default, producing a real
    (non-null) voltage point instead of every bucket in range coming
    back as an explicit gap."""
    range_start = _NOW_TS - 600
    samples = [(range_start, _reading(grid_v=60.0, grid_hz=59.9))]
    client, _db, _device_id = _client(
        tmp_path, samples=samples, detector_config=DetectorConfig(threshold_v=100.0)
    )

    response = client.get(
        "/api/v1/grid/series", params={"from": range_start, "to": range_start + 600}
    )

    points = response.json()["points"]
    assert len(points) == 2
    assert all(point["grid_v_avg"] is None for point in points)


def test_grid_series_reports_an_outage_bucket_as_an_explicit_null_point(
    tmp_path: Path,
) -> None:
    """F3 (orchestrator QA batch F): a bucket with no grid-present
    reading at all used to be skipped from `points` entirely, leaving
    a silent hole in the time-ordered array -- with no explicit
    `null`, the chart's own `connectNulls: false` has nothing to break
    on, so ECharts draws a straight connecting line across the outage
    instead of a visible gap. Pass-2: reverting the route's bucket
    loop to `continue` on an "unavailable" bucket (the pre-fix
    behavior) turns this red by dropping the middle bucket from
    `points` instead of emitting it with null voltage/frequency."""
    range_start = _NOW_TS - 900
    samples = [
        (range_start, _reading(grid_v=120.0, grid_hz=60.0)),  # bucket 0: present
        (range_start + 300, _reading(grid_v=10.0, grid_hz=None)),  # bucket 1: outage
        (range_start + 600, _reading(grid_v=121.0, grid_hz=60.0)),  # bucket 2: present
    ]
    client, _db, _device_id = _client(tmp_path, samples=samples)

    response = client.get(
        "/api/v1/grid/series", params={"from": range_start, "to": range_start + 900}
    )

    points = response.json()["points"]
    assert len(points) == 3
    assert points[0]["grid_v_avg"] == 120.0
    assert points[1]["grid_v_avg"] is None
    assert points[1]["grid_hz_avg"] is None
    assert points[2]["grid_v_avg"] == 121.0


# --- end-to-end: the real derive path persists, the real route serves --


def _never_ticks(application: object) -> object:
    from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle

    async def _blocks_forever() -> None:
        await asyncio.Event().wait()

    supervised = SupervisedTask(
        name="test-job",
        target=_blocks_forever,
        clock=application.clock,  # type: ignore[attr-defined]
    )
    task = asyncio.create_task(supervised.run())
    return SupervisedTaskHandle(supervised=supervised, task=task)


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def test_the_real_derive_path_persists_grid_ranges_the_api_then_serves_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gap class flagged twice before in this project: a fully
    unit-tested pure-core computation with zero production callers. This
    test seeds raw samples, runs the real `rollups.service.derive_rollups`
    (the exact function the hourly job already calls), then hits the
    real `GET /api/v1/grid/daily` route through a `TestClient` built from
    the real composition root, proving the number the API returns is
    the one the real derive path actually computed and persisted --
    never a hand-seeded rollup row."""
    from ecoflow_stats import bootstrap
    from ecoflow_stats.config import load_settings
    from ecoflow_stats.rollups.service import derive_rollups
    from ecoflow_stats.storage.derivations import DerivationStore
    from ecoflow_stats.web.app import create_app
    from tests.fakes import FakeClock

    env = {
        "ECOFLOW_ACCESS_KEY": "test-access-key",
        "ECOFLOW_SECRET_KEY": "test-secret-key",
        "ECOFLOW_DEVICES": "TESTDEV0001",
        "ECOFLOW_STATS_DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(os.environ)
    now = datetime.fromtimestamp(_NOW_TS, tz=UTC)
    application = bootstrap.build(settings, clock=FakeClock(now))
    (device,) = application.device_records

    sample_store = SampleStore(application.database.writer)
    sample_store.add(device.id, _NOW_TS - 120, 1, _reading(grid_v=118.0, grid_hz=59.8))
    sample_store.add(device.id, _NOW_TS - 60, 1, _reading(grid_v=122.0, grid_hz=60.2))
    application.database.writer.commit()

    application.database.writer.execute("BEGIN IMMEDIATE")
    derive_rollups(
        device.id,
        sample_store=sample_store,
        rollup_store=RollupStore(application.database.writer),
        derivation_store=DerivationStore(application.database.writer),
        now=now,
    )
    application.database.writer.commit()

    app = create_app(
        application,
        start_collector=_never_ticks,
        start_derive_job=_never_ticks,
        start_rollups_job=_never_ticks,
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/grid/daily", params={"from": _NOW_TS - 86_400, "to": _NOW_TS}
        )

    body = response.json()
    today = next(row for row in body["days"] if row["readings"] == 2)
    assert (today["grid_v_min"], today["grid_v_max"]) == (118.0, 122.0)
