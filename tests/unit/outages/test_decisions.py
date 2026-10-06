"""Tests for the decision-recording half of `outages.service`: a user's
confirm/reject/un-flag verdict on a gap or a legacy outage event.

Outages requirement "A User Decision on a Gap Is Durable and Survives
Recomputation" (all 3 scenarios: confirmed, rejected, and un-flagged
phantom each survive a detector re-run); design D4 ("undo/change keeps
the audit trail").
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.outages.service import derive_outages, record_decision, undo_decision
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.runs import RunLog
from ecoflow_stats.storage.samples import SampleStore

_CONFIG = DetectorConfig()
_NOW = datetime(2026, 10, 6, tzinfo=UTC)


class _Env:
    def __init__(self, db_path: Path) -> None:
        self.db = Database(db_path)
        self.device_id = (
            DeviceStore(self.db.writer)
            .upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1)
            .id
        )
        self.sample_store = SampleStore(self.db.writer)
        self.failure_log = FailureLog(self.db.writer)
        self.run_log = RunLog(self.db.writer)
        self.outage_store = OutageStore(self.db.writer)
        self.derivation_store = DerivationStore(self.db.writer)
        self.decision_store = DecisionStore(self.db.writer)

    def recompute(self) -> None:
        self.db.writer.execute("BEGIN IMMEDIATE")
        try:
            derive_outages(
                self.device_id,
                sample_store=self.sample_store,
                failure_log=self.failure_log,
                run_log=self.run_log,
                outage_store=self.outage_store,
                derivation_store=self.derivation_store,
                config=_CONFIG,
                now=_NOW,
            )
        except Exception:
            self.db.writer.rollback()
            raise
        self.db.writer.commit()

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[_Env]:
    environment = _Env(tmp_path / "ecoflow-stats.db")
    environment.sample_store.add(environment.device_id, 0, 1, Reading(grid_v=120.0, soc=90))
    yield environment
    environment.close()


def test_confirming_a_gap_persists_an_outage_verdict_anchored_to_its_interval(env: _Env) -> None:
    """Pass-1: without a stored decision anchored to the gap's own
    interval, a confirmed gap would have nothing durable for `resolve()`
    to later match it against -- the confirmation would be forgotten the
    moment the page reloads."""
    decision_id = record_decision(
        env.device_id,
        target="gap",
        start_ts=100,
        end_ts=400,
        verdict="outage",
        decision_store=env.decision_store,
        now=_NOW,
    )

    active = env.decision_store.active(env.device_id)
    assert len(active) == 1
    assert active[0].id == decision_id
    assert active[0].target == "gap"
    assert (active[0].start_ts, active[0].end_ts) == (100, 400)
    assert active[0].verdict == "outage"


def test_rejecting_a_gap_persists_a_no_outage_verdict(env: _Env) -> None:
    record_decision(
        env.device_id,
        target="gap",
        start_ts=100,
        end_ts=400,
        verdict="no_outage",
        decision_store=env.decision_store,
        now=_NOW,
    )

    active = env.decision_store.active(env.device_id)
    assert active[0].verdict == "no_outage"


def test_unflagging_a_suspected_phantom_persists_a_real_verdict_on_the_legacy_target(
    env: _Env,
) -> None:
    record_decision(
        env.device_id,
        target="legacy",
        start_ts=500,
        end_ts=500,
        verdict="real",
        decision_store=env.decision_store,
        now=_NOW,
    )

    active = env.decision_store.active(env.device_id)
    assert active[0].target == "legacy"
    assert active[0].verdict == "real"


@pytest.mark.parametrize(
    ("target", "verdict"),
    [("gap", "real"), ("gap", "phantom"), ("legacy", "outage"), ("legacy", "no_outage")],
)
def test_a_verdict_not_valid_for_its_target_is_rejected(
    env: _Env, target: str, verdict: str
) -> None:
    """Pass-1: without this guard, a caller could record a "phantom" gap
    or an "outage" legacy verdict -- values `resolve()` (task 11.9/11.10)
    is never written to handle for that target, silently producing
    nonsense instead of a clear error at the point of the mistake."""
    with pytest.raises(ValueError, match=target):
        record_decision(
            env.device_id,
            target=target,  # type: ignore[arg-type]
            start_ts=100,
            end_ts=400,
            verdict=verdict,  # type: ignore[arg-type]
            decision_store=env.decision_store,
            now=_NOW,
        )


def test_each_decision_survives_a_detector_recompute(env: _Env) -> None:
    """Outages requirement "A User Decision on a Gap Is Durable and
    Survives Recomputation": re-running the detector must never clear,
    overwrite, or duplicate an already-recorded decision -- `derive_outages`
    only ever touches `outage_events`/`gaps`/`derivations`, never
    `decisions`."""
    gap_decision = record_decision(
        env.device_id,
        target="gap",
        start_ts=100,
        end_ts=400,
        verdict="outage",
        decision_store=env.decision_store,
        now=_NOW,
    )
    legacy_decision = record_decision(
        env.device_id,
        target="legacy",
        start_ts=500,
        end_ts=500,
        verdict="phantom",
        decision_store=env.decision_store,
        now=_NOW,
    )

    env.recompute()
    env.recompute()  # a second run, with a new detector version too

    active = {d.id: d.verdict for d in env.decision_store.active(env.device_id)}
    assert active == {gap_decision: "outage", legacy_decision: "phantom"}


def test_undoing_a_decision_supersedes_it_rather_than_deleting_it(env: _Env) -> None:
    decision_id = record_decision(
        env.device_id,
        target="gap",
        start_ts=100,
        end_ts=400,
        verdict="outage",
        decision_store=env.decision_store,
        now=_NOW,
    )

    undo_decision(decision_id, decision_store=env.decision_store)

    assert env.decision_store.active(env.device_id) == []
    row = env.db.writer.execute(
        "SELECT superseded_by FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["superseded_by"] == decision_id
