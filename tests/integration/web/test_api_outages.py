"""Integration tests for the 4 outage-aggregate read-only API routes
(outages work unit 7a, PR i): `GET /api/v1/outages`, `outages/heatmap`,
`gaps`, `mains-strip`.

A standalone `FastAPI()` with only the api router mounted and
`app.state.api` set directly to a hand-built `ApiContext` against real
SQLite stores -- no real collector, no real composition root. Mirrors
`test_api_status.py`'s own harness exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ecoflow_stats.outages.model import Decision, DetectorConfig, Event, Gap
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.imports import ImportRunStore
from ecoflow_stats.storage.legacy import LegacyStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.routes.api import ApiContext, router


@dataclass(frozen=True, slots=True)
class _StubDatabase:
    """The one attribute `web.routes.api._legacy_store` reads off the
    real `bootstrap.Application` (`application.database.writer`) --
    this harness mounts only the `api` router against a hand-built
    `ApiContext`, with no real composition root, so it stands in for
    just that one cross-context read rather than constructing a full
    `Application`."""

    writer: object


@dataclass(frozen=True, slots=True)
class _StubApplication:
    database: _StubDatabase


_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_RANGE_START = _NOW_TS - 7 * 24 * 60 * 60
_RANGE_END = _NOW_TS


def _event(start_ts: int, end_ts: int | None, **overrides: object) -> Event:
    defaults: dict[str, object] = {"kind": "outage"}
    defaults.update(overrides)
    return Event(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


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


def _decision(device_id: int, target: str, start_ts: int, end_ts: int, verdict: str) -> Decision:
    return Decision(
        device_id=device_id,
        target=target,  # type: ignore[arg-type]
        start_ts=start_ts,
        end_ts=end_ts,
        verdict=verdict,  # type: ignore[arg-type]
        decided_at=1,
    )


def _client(
    tmp_path: Path, *, events: list[Event] | None = None, gaps: list[Gap] | None = None
) -> tuple[TestClient, Database, int]:
    events = events if events is not None else []
    gaps = gaps if gaps is not None else []
    db = Database(tmp_path / "ecoflow-stats.db")
    record = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    outage_store = OutageStore(db.writer)
    outage_store.replace_from(record.id, None, events, gaps)
    db.writer.commit()

    app = FastAPI()
    app.state.api = ApiContext(
        now=lambda: _NOW,
        stale_threshold_s=180,
        poll_interval_s=60,
        detector_config=DetectorConfig(),
        sample_store=None,  # type: ignore[arg-type]
        outage_store=outage_store,
        device_records=(record,),
        decision_store=DecisionStore(db.writer),
        tz="UTC",
    )
    app.state.application = _StubApplication(database=_StubDatabase(writer=db.writer))
    app.include_router(router)
    return TestClient(app), db, record.id


def _range_query(device_id: int) -> dict[str, int]:
    return {"device": device_id, "from": _RANGE_START, "to": _RANGE_END}


def test_outages_route_returns_phase_11s_aggregates_for_the_selected_range(
    tmp_path: Path,
) -> None:
    """Pass-1: a route that forgot to call `resolve()`/`compute_aggregates()`
    correctly (or called them with the wrong events) would report the wrong
    count/downtime, silently breaking the outages summary (scenario "GET
    /api/v1/outages returns Phase 11's aggregates for a selected device/range")."""
    outage_start = _RANGE_START + 1_000
    client, db, device_id = _client(tmp_path, events=[_event(outage_start, outage_start + 600)])
    try:
        response = client.get("/api/v1/outages", params=_range_query(device_id))

        assert response.status_code == 200
        body = response.json()
        assert body["device_id"] == device_id
        assert body["count"] == 1
        assert body["total_downtime_s"] == 600
        assert body["longest_s"] == 600
    finally:
        db.close()


def test_outages_route_counts_a_real_legacy_outage_but_excludes_an_unconfirmed_phantom(
    tmp_path: Path,
) -> None:
    """DATA-02 (qa-report-data-01.md): legacy outages imported from
    `outages.log` were never reconciled into statistics at all --
    `_reconcile` hardcoded `legacy=()` because `LegacyStore` had no
    range query. Proves both halves end-to-end through the real
    `/api/v1/outages` route: a non-phantom legacy entry is counted,
    and a `suspected_phantom` one stays excluded until a decision
    confirms it (the same honesty `resolve()`'s own unit tests already
    prove for the pure function -- this proves the production wiring
    that used to skip it entirely)."""
    client, db, device_id = _client(tmp_path)
    try:
        import_id = ImportRunStore(db.writer).start(device_id, started_at=1, source_tz="UTC")
        legacy_store = LegacyStore(db.writer)
        real_start = _RANGE_START + 1_000
        legacy_store.upsert(
            device_id=device_id,
            start_ts=real_start,
            end_ts=real_start + 600,
            soc_start=90,
            soc_end=80,
            logged_minutes=10,
            start_line="corte",
            end_line="retorno",
            source_tz="UTC",
            flags=frozenset(),
            import_id=import_id,
        )
        phantom_start = _RANGE_START + 50_000
        legacy_store.upsert(
            device_id=device_id,
            start_ts=phantom_start,
            end_ts=phantom_start + 300,
            soc_start=0,
            soc_end=0,
            logged_minutes=5,
            start_line="corte",
            end_line="retorno",
            source_tz="UTC",
            flags=frozenset({"suspected_phantom"}),
            import_id=import_id,
        )

        response = client.get("/api/v1/outages", params=_range_query(device_id))

        assert response.status_code == 200
        body = response.json()
        # Only the real (non-phantom) legacy entry is counted.
        assert body["count"] == 1
        assert body["total_downtime_s"] == 600
    finally:
        db.close()


def test_heatmap_route_returns_the_weekday_and_hour_distribution(tmp_path: Path) -> None:
    """Pass-1: a route returning the wrong aggregate fields (or zeroed
    arrays) would silently break the heatmap chart's only data source
    (scenario "GET /api/v1/outages/heatmap returns the weekday x hour
    matrix")."""
    outage_start = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)
    outage_start_ts = int(outage_start.timestamp())
    client, db, device_id = _client(
        tmp_path, events=[_event(outage_start_ts, outage_start_ts + 60)]
    )
    try:
        response = client.get("/api/v1/outages/heatmap", params=_range_query(device_id))

        assert response.status_code == 200
        body = response.json()
        assert body["hour_of_day"][outage_start.hour] == 1
        assert body["day_of_week"][outage_start.weekday()] == 1
        assert sum(body["hour_of_day"]) == 1
        assert sum(body["day_of_week"]) == 1
    finally:
        db.close()


def test_gaps_route_returns_only_unresolved_gaps_with_their_evidence(
    tmp_path: Path,
) -> None:
    """Pass-1: a route that returned every gap regardless of decision
    would re-surface an already-reviewed gap in the review queue forever;
    one that dropped the evidence fields would leave a reviewer with
    nothing to decide from (scenario "GET /api/v1/gaps returns unresolved
    gaps with before/after evidence")."""
    unresolved_start = _RANGE_START + 2_000
    resolved_start = _RANGE_START + 5_000
    client, db, device_id = _client(
        tmp_path,
        gaps=[
            _gap(unresolved_start, unresolved_start + 300, soc_before=90, soc_after=88),
            _gap(resolved_start, resolved_start + 300),
        ],
    )
    try:
        DecisionStore(db.writer).add(
            _decision(device_id, "gap", resolved_start, resolved_start + 300, "outage")
        )

        response = client.get("/api/v1/gaps", params=_range_query(device_id))

        assert response.status_code == 200
        body = response.json()
        assert len(body["gaps"]) == 1
        gap = body["gaps"][0]
        assert gap["start_ts"] == unresolved_start
        assert gap["soc_before"] == 90
        assert gap["soc_after"] == 88
        assert gap["evidence"] == "inconclusive"
    finally:
        db.close()


def test_mains_strip_route_returns_a_run_length_series_covering_the_range(
    tmp_path: Path,
) -> None:
    """Pass-1: a route that ignored the outage/gap data (e.g. always
    returning one all-present segment) would defeat the mains strip's
    entire purpose -- showing a real cut and real unknown time, not a
    fabricated all-clear (scenario "GET /api/v1/mains-strip returns a
    run-length [start, end, state] series")."""
    outage_start = _RANGE_START + 1_000
    outage_end = outage_start + 600
    gap_start = _RANGE_START + 10_000
    gap_end = gap_start + 400
    client, db, device_id = _client(
        tmp_path,
        events=[_event(outage_start, outage_end)],
        gaps=[_gap(gap_start, gap_end)],
    )
    try:
        response = client.get("/api/v1/mains-strip", params=_range_query(device_id))

        assert response.status_code == 200
        body = response.json()
        series = body["series"]

        # The full range is covered with no holes or overlaps.
        assert series[0][0] == _RANGE_START
        assert series[-1][1] == _RANGE_END
        for (_, end, _state), (next_start, _, _next_state) in pairwise(series):
            assert end == next_start

        absent = [seg for seg in series if seg[2] == "absent"]
        unknown = [seg for seg in series if seg[2] == "unknown"]
        assert absent == [[outage_start, outage_end, "absent"]]
        assert unknown == [[gap_start, gap_end, "unknown"]]
    finally:
        db.close()


def test_every_new_route_is_get_only_with_no_side_effect(tmp_path: Path) -> None:
    """Pass-1: a stray `@router.post` (or a route that mutates storage on
    a GET) would violate "every route is GET-only with no side effect" --
    a real risk here since every sibling review-action route landing in
    the next phase *is* a POST on a neighboring path (scenario "Every one
    of these 4 routes is GET-only with no side effect")."""
    outage_start = _RANGE_START + 1_000
    client, db, device_id = _client(tmp_path, events=[_event(outage_start, outage_start + 600)])
    try:
        paths = (
            "/api/v1/outages",
            "/api/v1/outages/heatmap",
            "/api/v1/gaps",
            "/api/v1/mains-strip",
        )
        query = _range_query(device_id)

        for path in paths:
            post_response = client.post(path, params=query)
            assert post_response.status_code == 405

        before = {path: client.get(path, params=query).json() for path in paths}
        after = {path: client.get(path, params=query).json() for path in paths}
        assert before == after

        events_after = outage_store_events_count(db)
        assert events_after == 1
    finally:
        db.close()


def outage_store_events_count(db: Database) -> int:
    row = db.writer.execute("SELECT COUNT(*) AS n FROM outage_events").fetchone()
    return int(row["n"])
