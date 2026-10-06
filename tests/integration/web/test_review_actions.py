"""Integration tests for the gap and phantom review actions (task 16,
web-ui "Gap and Phantom Review Flow"): the HTMX-expandable gap-review
list, the gap confirm/reject decision route, the legacy un-flag-as-real
route, and the shared undo route -- each requiring the Phase 14 CSRF
dependency.

Built through the real `create_app`/`bootstrap.build` composition root,
the same harness `test_outages_page.py` and `test_csrf.py` already use,
so the routes are proven wired end-to-end, including their storage
persistence, not just against a hand-built stub.
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
from ecoflow_stats.outages.model import Decision, Gap
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.imports import ImportRunStore
from ecoflow_stats.storage.legacy import LegacyStore
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.web.app import create_app
from tests.fakes import FakeClock

_NOW = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())
_RANGE_START = _NOW_TS - 7 * 24 * 60 * 60


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


def _seed_legacy_phantom(application: bootstrap.Application, device_id: int, start_ts: int) -> None:
    import_id = ImportRunStore(application.database.writer).start(device_id, _NOW_TS, "UTC")
    LegacyStore(application.database.writer).upsert(
        device_id=device_id,
        start_ts=start_ts,
        end_ts=start_ts + 120,
        soc_start=0,
        soc_end=0,
        logged_minutes=2,
        start_line="corte 00:00",
        end_line="vuelve 00:02",
        source_tz="UTC",
        flags=frozenset({"suspected_phantom"}),
        import_id=import_id,
    )


def test_opening_the_gap_review_list_shows_only_unresolved_gaps_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A gap can be reviewed and resolved" (the listing half):
    Pass-1, a route listing every gap regardless of decision would
    re-surface an already-reviewed gap in the review list forever, and
    one that recomputed evidence instead of reusing `make_gap`'s stored
    fields could silently disagree with what was actually detected."""
    unresolved_start = _RANGE_START + 1_000
    decided_start = _RANGE_START + 5_000
    application, device_id, client = _client(
        monkeypatch,
        tmp_path,
        gaps=[
            _gap(unresolved_start, unresolved_start + 300, soc_before=90, soc_after=88),
            _gap(decided_start, decided_start + 300),
        ],
    )
    decision_store = DecisionStore(application.database.writer)
    decision_store.add(
        Decision(
            device_id=device_id,
            target="gap",
            start_ts=decided_start,
            end_ts=decided_start + 300,
            verdict="outage",
            decided_at=1,
        )
    )
    try:
        with client:
            response = client.get(f"/outages/gaps?device={device_id}")

        assert response.status_code == 200
        html = response.text
        assert f'data-gap-start="{unresolved_start}"' in html
        assert f'data-gap-start="{decided_start}"' not in html
        assert "Charge before" in html and "90" in html
        assert "Confirm as outage" in html
        assert "Reject (not an outage)" in html
    finally:
        application.database.close()


def test_confirming_a_gap_persists_the_outage_verdict_and_returns_the_updated_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A gap can be reviewed and resolved" (the confirm half):
    Pass-1, a route that renders a success response without actually
    calling `record_decision` would show a confirmed row that the
    database never actually recorded -- the next full recompute would
    silently lose it."""
    gap_start = _RANGE_START + 2_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            response = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage"},
            )

        assert response.status_code == 200
        assert response.headers.get("hx-trigger") == "outages-changed"
        assert "Confirmed as an outage" in response.text
        assert "/decisions/" in response.text

        decisions = DecisionStore(application.database.writer).active(device_id)
        assert len(decisions) == 1
        assert decisions[0].target == "gap"
        assert decisions[0].start_ts == gap_start
        assert decisions[0].verdict == "outage"
    finally:
        application.database.close()


def test_rejecting_a_gap_persists_the_no_outage_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a route that only ever recorded "outage" regardless of
    which button was pressed would make the reject action silently
    behave like confirm."""
    gap_start = _RANGE_START + 3_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            response = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "no_outage"},
            )

        assert response.status_code == 200
        assert "Rejected" in response.text

        decisions = DecisionStore(application.database.writer).active(device_id)
        assert len(decisions) == 1
        assert decisions[0].verdict == "no_outage"
    finally:
        application.database.close()


def test_undo_reverts_a_gap_decision_to_its_prior_unresolved_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario covering `POST /decisions/{id}/undo`: Pass-1, a route
    that rendered the undecided row without actually calling
    `undo_decision` would leave the stale decision active in storage,
    so the gap would stay excluded from the review queue even though
    the UI claims it is unresolved again."""
    gap_start = _RANGE_START + 4_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            confirm = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage"},
            )
            decision_id = DecisionStore(application.database.writer).active(device_id)[0].id

            undo_response = client.post(
                f"/decisions/{decision_id}/undo",
                data={"device": device_id, "target": "gap", "start": gap_start},
            )

        assert confirm.status_code == 200
        assert undo_response.status_code == 200
        assert "Confirm as outage" in undo_response.text
        assert "Reject (not an outage)" in undo_response.text

        assert DecisionStore(application.database.writer).active(device_id) == []
    finally:
        application.database.close()


def test_unflagging_a_suspected_phantom_shows_it_as_confirmed_real(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A suspected phantom can be un-flagged": Pass-1, a route
    that recorded the "real" verdict under the wrong target (e.g.
    "gap" instead of "legacy") would never actually un-flag the
    phantom -- `resolve()` keys its phantom check on `target="legacy"`."""
    legacy_start = _RANGE_START + 6_000
    application, device_id, client = _client(monkeypatch, tmp_path)
    _seed_legacy_phantom(application, device_id, legacy_start)
    try:
        with client:
            response = client.post(
                f"/outages/legacy/{legacy_start}/decision",
                data={"device": device_id, "verdict": "real"},
            )

        assert response.status_code == 200
        assert response.headers.get("hx-trigger") == "outages-changed"
        assert "Confirmed real" in response.text
        assert "Suspected phantom" not in response.text

        decisions = DecisionStore(application.database.writer).active(device_id)
        assert len(decisions) == 1
        assert decisions[0].target == "legacy"
        assert decisions[0].start_ts == legacy_start
        assert decisions[0].verdict == "real"
    finally:
        application.database.close()


def test_a_decision_for_a_non_matching_gap_start_is_rejected_without_recording_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pass-1: a lookup that used `OutageStore.gaps`'s range-overlap
    query alone, without also filtering for the exact `start_ts`, would
    match this real, still-open gap even though the requested
    `gap_start` falls strictly inside it rather than at its start --
    silently anchoring the recorded decision to the wrong interval."""
    real_start = _RANGE_START + 8_000
    requested_start = real_start + 100  # inside the gap's window, not its start
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(real_start, real_start + 300)]
    )
    try:
        with client:
            response = client.post(
                f"/outages/gaps/{requested_start}/decision",
                data={"device": device_id, "verdict": "outage"},
            )

        assert response.status_code == 404
        assert DecisionStore(application.database.writer).active(device_id) == []
    finally:
        application.database.close()


def test_a_cross_site_post_to_a_gap_decision_route_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Named Defect "CSRF gap", applied to this new route: Pass-1, a
    route missing the `require_csrf` dependency would accept a forged
    cross-site POST, exactly like a page without any CSRF protection at
    all."""
    gap_start = _RANGE_START + 7_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            response = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage"},
                headers={"origin": "http://evil.example"},
            )

        assert response.status_code == 403
        assert DecisionStore(application.database.writer).active(device_id) == []
    finally:
        application.database.close()
