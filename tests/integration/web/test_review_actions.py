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
import re
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
from ecoflow_stats.web.security import CSRF_COOKIE, csrf_token
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


def _csrf_headers(client: TestClient, application: bootstrap.Application) -> dict[str, str]:
    """SEC-01 made `require_csrf` fail closed: every mutating POST in
    this file now needs real CSRF proof. Mints the cookie via the
    overview page (`/`, wired since batch12, unaffected by this
    slice's own outages/battery-page changes) rather than `/outages`,
    so this helper's correctness never depends on the very fix it is
    exercising."""
    client.get("/")
    return {"x-csrf-token": csrf_token(application.secret, client.cookies[CSRF_COOKIE])}


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


def test_opening_the_gap_review_list_shows_unresolved_and_decided_gaps_with_their_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scenario "A gap can be reviewed and resolved" (the listing half):
    Pass-1, a route listing every gap regardless of decision would
    re-surface an already-reviewed gap's confirm/reject forms forever,
    and one that recomputed evidence instead of reusing `make_gap`'s
    stored fields could silently disagree with what was actually
    detected.

    Also proves UI-12 (qa-report-ui-01.md): a decided gap used to
    disappear from this list entirely the moment it was decided --
    this fetches the list fresh (no state carried from deciding it)
    and still finds the decided gap's row, its recorded verdict text,
    and its own undo form naming the real decision id.

    Also proves UI-16 (qa-report-ui-01.md: the "Cause" label was shown
    twice -- once as the `<dt>`, again inside the `<dd>`'s own value,
    e.g. "Cause" / "Cause: unknown"): the `<dd>` must show only the
    cause itself."""
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
    decision_id = decision_store.add(
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
        assert "Charge before" in html and "90" in html
        assert "Confirm as outage" in html
        assert "Reject (not an outage)" in html

        assert "Cause: unknown" not in html
        assert re.search(r"<dt>Cause</dt>\s*<dd>unknown</dd>", html) is not None

        assert f'data-gap-start="{decided_start}"' in html
        assert "Confirmed as an outage" in html
        assert f"/decisions/{decision_id}/undo" in html
    finally:
        application.database.close()


def test_a_decided_gaps_undo_is_reachable_from_the_review_list_after_a_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-12 (qa-report-ui-01.md): "a decided gap disappears from the
    review list, making its undo unreachable after a reload". This
    decides a gap, then -- deliberately never reusing that response --
    opens a brand new GET of the list (what a page reload actually
    does), extracts the undo form's decision id purely from THAT fresh
    HTML, and proves posting to it actually works: the gap goes back to
    showing its confirm/reject forms, exactly like `test_undo_reverts_
    a_gap_decision_to_its_prior_unresolved_state` already proves for an
    undo performed without a reload in between."""
    gap_start = _RANGE_START + 10_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            headers = _csrf_headers(client, application)
            client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage"},
                headers=headers,
            )

            reload_response = client.get(f"/outages/gaps?device={device_id}")
            reload_html = reload_response.text
            match = re.search(r"/decisions/(\d+)/undo", reload_html)
            assert match is not None, "no undo form for the decided gap after reload"
            decision_id = int(match.group(1))

            undo_response = client.post(
                f"/decisions/{decision_id}/undo",
                data={
                    "device": device_id,
                    "target": "gap",
                    "start": gap_start,
                    "csrf_token": headers["x-csrf-token"],
                },
                headers=headers,
            )

        assert undo_response.status_code == 200
        assert "Confirm as outage" in undo_response.text
        assert "Reject (not an outage)" in undo_response.text
        assert DecisionStore(application.database.writer).active(device_id) == []
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
                headers=_csrf_headers(client, application),
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
                headers=_csrf_headers(client, application),
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
            headers = _csrf_headers(client, application)
            confirm = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage"},
                headers=headers,
            )
            decision_id = DecisionStore(application.database.writer).active(device_id)[0].id

            undo_response = client.post(
                f"/decisions/{decision_id}/undo",
                data={"device": device_id, "target": "gap", "start": gap_start},
                headers=headers,
            )

        assert confirm.status_code == 200
        assert undo_response.status_code == 200
        assert "Confirm as outage" in undo_response.text
        assert "Reject (not an outage)" in undo_response.text

        assert DecisionStore(application.database.writer).active(device_id) == []
    finally:
        application.database.close()


def test_deciding_a_gap_oob_swaps_the_pending_count_in_the_same_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI2-06 (qa-report-ui-02.md): the gap-review pending count used
    to stay stale until the next reload -- deciding a gap only ever
    re-rendered that one row, never the count text above the list.
    Pass-1: a route that recomputed the count but forgot to emit it
    as an `hx-swap-oob` fragment (or emitted it under the wrong `id`)
    would leave htmx with nothing to swap, silently reproducing the
    exact staleness this finding reports -- this test starts with two
    pending gaps, decides one, and asserts the *same* response already
    carries the updated "1 gap awaiting review" text against the
    `gap-review-count` id, with no second request."""
    first_start = _RANGE_START + 2_000
    second_start = _RANGE_START + 5_000
    application, device_id, client = _client(
        monkeypatch,
        tmp_path,
        gaps=[
            _gap(first_start, first_start + 90),
            _gap(second_start, second_start + 90),
        ],
    )
    try:
        with client:
            headers = _csrf_headers(client, application)
            list_response = client.get(
                "/outages/gaps",
                params={"device": device_id, "from": _RANGE_START, "to": _NOW_TS},
            )
            assert "2 gaps awaiting review" not in list_response.text  # the list has no count text

            response = client.post(
                f"/outages/gaps/{first_start}/decision",
                data={
                    "device": device_id,
                    "verdict": "outage",
                    "range_from": _RANGE_START,
                    "range_to": _NOW_TS,
                },
                headers=headers,
            )

        assert response.status_code == 200
        assert 'id="gap-review-count"' in response.text
        assert 'hx-swap-oob="true"' in response.text
        assert "1 gap awaiting review" in response.text
        assert "2 gaps awaiting review" not in response.text
    finally:
        application.database.close()


def test_undoing_a_gap_decision_oob_swaps_the_pending_count_back_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI2-06: the inverse of the test above -- undoing a decision
    makes a gap pending again, so the count it was removed from must
    go back up in the undo's own response too, not just the decide
    response. Pass-2 target: an `undo` that re-rendered the row but
    never recomputed/emitted the OOB count fragment (the state before
    this fix) turns this red -- no `gap-review-count` text appears in
    `undo_response.text` at all."""
    gap_start = _RANGE_START + 2_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            headers = _csrf_headers(client, application)
            client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={
                    "device": device_id,
                    "verdict": "outage",
                    "range_from": _RANGE_START,
                    "range_to": _NOW_TS,
                },
                headers=headers,
            )
            decision_id = DecisionStore(application.database.writer).active(device_id)[0].id

            undo_response = client.post(
                f"/decisions/{decision_id}/undo",
                data={
                    "device": device_id,
                    "target": "gap",
                    "start": gap_start,
                    "range_from": _RANGE_START,
                    "range_to": _NOW_TS,
                },
                headers=headers,
            )

        assert undo_response.status_code == 200
        assert 'id="gap-review-count"' in undo_response.text
        assert "1 gap awaiting review" in undo_response.text
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
                headers=_csrf_headers(client, application),
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


def test_deciding_a_legacy_entry_oob_swaps_the_pending_count_in_the_same_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI2-06 (qa-report-ui-02.md): `decide_legacy`'s own OOB pending-
    count update, mirroring the gap test above -- a different code
    path (`LegacyStore.between` + `unresolved_legacy`, not
    `OutageStore.gaps` + `unresolved_gaps`), so it is not proven by
    that test alone. Starts with two suspected phantoms, un-flags one,
    and asserts the same response already shows "1 legacy outage
    awaiting review" against the `legacy-review-count` id."""
    first_start = _RANGE_START + 6_000
    second_start = _RANGE_START + 9_000
    application, device_id, client = _client(monkeypatch, tmp_path)
    _seed_legacy_phantom(application, device_id, first_start)
    _seed_legacy_phantom(application, device_id, second_start)
    try:
        with client:
            response = client.post(
                f"/outages/legacy/{first_start}/decision",
                data={
                    "device": device_id,
                    "verdict": "real",
                    "range_from": _RANGE_START,
                    "range_to": _NOW_TS,
                },
                headers=_csrf_headers(client, application),
            )

        assert response.status_code == 200
        assert 'id="legacy-review-count"' in response.text
        assert 'hx-swap-oob="true"' in response.text
        assert "1 legacy outage awaiting review" in response.text
        assert "2 legacy outages awaiting review" not in response.text
    finally:
        application.database.close()


def test_opening_the_legacy_review_list_shows_unresolved_and_decided_legacy_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-11 (qa-report-ui-01.md): the legacy decide/undo routes already
    existed with no UI entry point reaching them at all -- this proves
    the missing `GET /outages/legacy` list route itself, mirroring
    `test_opening_the_gap_review_list_shows_unresolved_and_decided_gaps_
    with_their_evidence` exactly: a still-unconfirmed suspected-phantom
    entry shows its "mark as real" form, and an entry already confirmed
    real shows its recorded verdict and its own undo form naming the
    real decision id -- never disappearing from the list once decided,
    exactly like a decided gap doesn't (UI-12's same fix, extended)."""
    unresolved_start = _RANGE_START + 2_000
    decided_start = _RANGE_START + 8_000
    application, device_id, client = _client(monkeypatch, tmp_path)
    _seed_legacy_phantom(application, device_id, unresolved_start)
    _seed_legacy_phantom(application, device_id, decided_start)
    decision_store = DecisionStore(application.database.writer)
    decision_id = decision_store.add(
        Decision(
            device_id=device_id,
            target="legacy",
            start_ts=decided_start,
            end_ts=decided_start + 120,
            verdict="real",
            decided_at=1,
        )
    )
    try:
        with client:
            response = client.get(f"/outages/legacy?device={device_id}")

        assert response.status_code == 200
        html = response.text
        assert f'data-legacy-start="{unresolved_start}"' in html
        assert "Mark as real" in html
        assert "Suspected phantom" in html

        assert f'data-legacy-start="{decided_start}"' in html
        assert "Confirmed real" in html
        assert f"/decisions/{decision_id}/undo" in html
    finally:
        application.database.close()


def test_a_decided_legacy_entrys_undo_is_reachable_from_the_review_list_after_a_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UI-11: mirrors `test_a_decided_gaps_undo_is_reachable_from_the_
    review_list_after_a_reload` for a legacy entry -- confirms a
    suspected phantom as real, reloads the list fresh (never reusing
    the decide response), extracts the undo form's decision id purely
    from that fresh HTML, and proves posting to it actually reverts the
    entry back to its unconfirmed phantom state."""
    legacy_start = _RANGE_START + 9_000
    application, device_id, client = _client(monkeypatch, tmp_path)
    _seed_legacy_phantom(application, device_id, legacy_start)
    try:
        with client:
            headers = _csrf_headers(client, application)
            client.post(
                f"/outages/legacy/{legacy_start}/decision",
                data={"device": device_id, "verdict": "real"},
                headers=headers,
            )

            reload_response = client.get(f"/outages/legacy?device={device_id}")
            reload_html = reload_response.text
            match = re.search(r"/decisions/(\d+)/undo", reload_html)
            assert match is not None, "no undo form for the decided legacy entry after reload"
            decision_id = int(match.group(1))

            undo_response = client.post(
                f"/decisions/{decision_id}/undo",
                data={
                    "device": device_id,
                    "target": "legacy",
                    "start": legacy_start,
                    "csrf_token": headers["x-csrf-token"],
                },
                headers=headers,
            )

        assert undo_response.status_code == 200
        assert "Suspected phantom" in undo_response.text
        assert DecisionStore(application.database.writer).active(device_id) == []
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
                headers=_csrf_headers(client, application),
            )

        assert response.status_code == 404
        assert DecisionStore(application.database.writer).active(device_id) == []
    finally:
        application.database.close()


def test_the_gap_review_rows_own_rendered_csrf_field_is_accepted_when_submitted_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEC-01 (qa-report-data-01.md): this is the report's own exact
    repro route (`POST /outages/gaps/{gap_start}/decision`), replayed
    through the legitimate UI path instead of a hand-crafted header --
    scraping the real token `gap_row.html`'s own confirm form rendered,
    not one computed independently, so a regression that drops the
    hidden field from that specific template (while `base.html`'s nav
    forms and `login.html` stay untouched) is caught here and nowhere
    else. Also proves the fix does not just block forgeries but leaves
    the real gap-review feature working end-to-end."""
    gap_start = _RANGE_START + 9_000
    application, device_id, client = _client(
        monkeypatch, tmp_path, gaps=[_gap(gap_start, gap_start + 90)]
    )
    try:
        with client:
            list_html = client.get(f"/outages/gaps?device={device_id}").text
            match = re.search(r'name="csrf_token" value="([^"]+)"', list_html)
            assert match is not None, "gap_row.html rendered no csrf_token field"
            token = match.group(1)

            response = client.post(
                f"/outages/gaps/{gap_start}/decision",
                data={"device": device_id, "verdict": "outage", "csrf_token": token},
            )

        assert response.status_code == 200
        assert response.headers.get("hx-trigger") == "outages-changed"
        assert "Confirmed as an outage" in response.text

        decisions = DecisionStore(application.database.writer).active(device_id)
        assert len(decisions) == 1
        assert decisions[0].verdict == "outage"
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
