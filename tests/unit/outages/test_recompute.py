"""Tests for `outages.service.derive_outages`: the recompute orchestration
that decides whether to resume from a checkpoint or recompute fully, and
that wires `detect()` to real storage inside one transaction per run.

Named Defects "Incremental drift" and "Gap as outage" (reconciliation
half); storage requirements "Outage Recomputation Is Versioned and
Re-Runnable" and "A User Decision on a Gap Is Durable and Survives
Recomputation" (persistence half -- decisions are never touched by this
module, proven directly here rather than only inferred from storage).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages import model
from ecoflow_stats.outages.detector import detect
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.outages.service import derive_outages
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.outages import OutageStore
from ecoflow_stats.storage.runs import RunLog
from ecoflow_stats.storage.samples import SampleStore

_CONFIG = DetectorConfig()
_NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _reading(grid_v: float | None, soc: int) -> Reading:
    # ``soc`` varies across every call site below precisely so no three
    # consecutive samples are byte-identical and trip the stale-payload
    # guard (amendment A3) -- the same fixture lesson slice 15 hit.
    return Reading(grid_v=grid_v, soc=soc)


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

    def insert(self, samples: list[tuple[int, Reading]]) -> None:
        for ts, reading in samples:
            self.sample_store.add(self.device_id, ts, 1, reading)

    def derive(self, *, now: datetime = _NOW, full: bool = False) -> bool:
        self.db.writer.execute("BEGIN IMMEDIATE")
        try:
            ran = derive_outages(
                self.device_id,
                sample_store=self.sample_store,
                failure_log=self.failure_log,
                run_log=self.run_log,
                outage_store=self.outage_store,
                derivation_store=self.derivation_store,
                config=_CONFIG,
                now=now,
                full=full,
            )
        except Exception:
            self.db.writer.rollback()
            raise
        self.db.writer.commit()
        return ran

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    created = _Env(tmp_path / "ecoflow-stats.db")
    yield created
    created.close()


def _present_and_outage_samples() -> list[tuple[int, Reading]]:
    """A present stretch, then a 2-reading official outage, then present
    again -- long enough to leave a clean `Quiescent` checkpoint at the
    end for every test below to build on."""
    return [
        (0, _reading(120.0, 90)),
        (60, _reading(120.0, 88)),
        (120, _reading(0.0, 86)),
        (180, _reading(0.0, 84)),
        (240, _reading(120.0, 83)),
        (300, _reading(120.0, 81)),
    ]


def test_a_detector_version_upgrade_records_the_new_version_without_losing_history(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario "A detector upgrade records the new version without
    losing history": raw samples stay byte-identical, every previously
    recorded decision keeps applying, and the recomputed events now carry
    the new version."""
    env.insert(_present_and_outage_samples())
    env.db.writer.execute(
        "INSERT INTO decisions (device_id, target, start_ts, end_ts, verdict, decided_at)"
        " VALUES (?, 'gap', 120, 240, 'outage', 1)",
        (env.device_id,),
    )
    env.db.writer.commit()
    assert env.derive() is True
    before_samples = list(env.sample_store.between(env.device_id, 0, 1_000))

    monkeypatch.setattr(model, "DETECTOR_VERSION", 2)
    assert env.derive(now=_NOW) is True

    events = env.outage_store.events(env.device_id, 0, 1_000)
    assert events
    assert all(e.detector_version == 2 for e in events)
    after_samples = list(env.sample_store.between(env.device_id, 0, 1_000))
    assert after_samples == before_samples
    (verdict,) = env.db.writer.execute(
        "SELECT verdict FROM decisions WHERE device_id = ? AND start_ts = 120", (env.device_id,)
    ).fetchone()
    assert verdict == "outage"


def test_recomputing_with_a_prior_version_reproduces_that_versions_output(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario "Recomputing with a prior version reproduces that
    version's output": rolling the active version back and recomputing
    must match a from-scratch `detect()` run over the same raw samples,
    not something drifted from the intermediate version's stored rows."""
    samples = _present_and_outage_samples()
    env.insert(samples)
    assert env.derive() is True

    monkeypatch.setattr(model, "DETECTOR_VERSION", 2)
    assert env.derive() is True
    monkeypatch.setattr(model, "DETECTOR_VERSION", 1)
    assert env.derive() is True

    events = env.outage_store.events(env.device_id, 0, 1_000)
    gaps = env.outage_store.gaps(env.device_id, 0, 1_000)
    assert all(e.detector_version == 1 for e in events)
    reference = detect(samples, config=_CONFIG)
    assert [e.start_ts for e in events] == [e.start_ts for e in reference.events]
    assert [e.readings for e in events] == [e.readings for e in reference.events]
    assert gaps == []


def test_an_incremental_recompute_matches_a_full_recompute_over_the_same_history(
    tmp_path: Path,
) -> None:
    """Named Defect "Incremental drift": resuming from the stored
    checkpoint (a collector tick's dirty mark) must produce the exact same
    stored events/gaps a full recompute over the whole, now-longer sample
    set would -- proven by running both paths independently and comparing
    their final stored rows, not just asserting a count."""
    first_batch = _present_and_outage_samples()
    second_batch = [
        (360, _reading(120.0, 79)),
        (420, _reading(0.0, 77)),
        (480, _reading(0.0, 75)),
        (540, _reading(120.0, 74)),
    ]

    incremental = _Env(tmp_path / "incremental.db")
    try:
        incremental.insert(first_batch)
        assert incremental.derive() is True
        incremental.insert(second_batch)
        incremental.derivation_store.mark_dirty(
            incremental.device_id, "outages", second_batch[0][0]
        )
        assert incremental.derive() is True
        incremental_events = incremental.outage_store.events(incremental.device_id, 0, 1_000)
        incremental_gaps = incremental.outage_store.gaps(incremental.device_id, 0, 1_000)
    finally:
        incremental.close()

    full = _Env(tmp_path / "full.db")
    try:
        full.insert(first_batch + second_batch)
        assert full.derive() is True
        full_events = full.outage_store.events(full.device_id, 0, 1_000)
        full_gaps = full.outage_store.gaps(full.device_id, 0, 1_000)
    finally:
        full.close()

    assert incremental_events == full_events
    assert incremental_gaps == full_gaps


def test_a_dirty_mark_before_the_checkpoint_forces_a_full_recompute(env: _Env) -> None:
    """Scenario covering the import trigger ("dirty_from = earliest
    imported ts"): when an import backfills samples older than the
    existing checkpoint, there is no known-good checkpoint at or before
    that earlier point, so the only correct choice is a full recompute --
    proven here by an outage that exists ONLY in the backfilled older
    samples actually showing up after recomputing."""
    env.insert(_present_and_outage_samples())
    assert env.derive() is True
    checkpoint = env.derivation_store.get(env.device_id, "outages")
    assert checkpoint is not None and checkpoint.checkpoint_ts is not None

    older_outage = [
        (-300, _reading(120.0, 95)),
        (-240, _reading(0.0, 94)),
        (-180, _reading(0.0, 93)),
        (-120, _reading(120.0, 92)),
    ]
    env.insert(older_outage)
    env.derivation_store.mark_dirty(env.device_id, "outages", older_outage[0][0])

    assert env.derive() is True

    events = env.outage_store.events(env.device_id, -1_000, 1_000)
    assert any(e.start_ts == -240 for e in events)


def test_a_clean_derivation_does_nothing_unless_forced(env: _Env) -> None:
    """Covers both the "startup, nothing changed" and ordinary idle-tick
    cases: a clean derivation (no dirty mark, same version and params)
    must be a true no-op, never re-stamping `computed_at`."""
    env.insert(_present_and_outage_samples())
    assert env.derive() is True
    computed_at_first = env.derivation_store.get(env.device_id, "outages").computed_at  # type: ignore[union-attr]

    ran = env.derive(now=datetime(2026, 10, 6, 1, tzinfo=UTC))

    assert ran is False
    computed_at_after = env.derivation_store.get(env.device_id, "outages").computed_at  # type: ignore[union-attr]
    assert computed_at_after == computed_at_first


def test_the_full_flag_forces_a_recompute_even_when_clean(env: _Env) -> None:
    """Scenario covering the CLI trigger ("CLI recompute command
    (full)"): `full=True` must force a fresh run even with nothing
    marked dirty -- the whole point of an operator-invoked recompute."""
    env.insert(_present_and_outage_samples())
    assert env.derive() is True
    computed_at_first = env.derivation_store.get(env.device_id, "outages").computed_at  # type: ignore[union-attr]

    ran = env.derive(now=datetime(2026, 10, 6, 1, tzinfo=UTC), full=True)

    assert ran is True
    computed_at_after = env.derivation_store.get(env.device_id, "outages").computed_at  # type: ignore[union-attr]
    assert computed_at_after != computed_at_first
