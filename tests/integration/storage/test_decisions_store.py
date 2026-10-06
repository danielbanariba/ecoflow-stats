"""Integration tests for `storage.decisions.DecisionStore` against real
SQLite.

Storage requirement: "A User Decision on a Gap Is Durable and Survives
Recomputation" (persistence half -- the detector-side survival is proven
directly in `tests/unit/outages/test_decisions.py`). This file proves the
store's own mechanics: a new decision supersedes the previously active
one for the same target/interval, `active()` only ever returns
not-yet-superseded rows, and `undo()` marks a row superseded by itself
without deleting it.
"""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.outages.model import Decision
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.decisions import DecisionStore
from ecoflow_stats.storage.devices import DeviceStore


def _env(tmp_path: Path) -> tuple[DecisionStore, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    return DecisionStore(db.writer), db, device.id


def _decision(device_id: int, **overrides: object) -> Decision:
    defaults: dict[str, object] = {
        "device_id": device_id,
        "target": "gap",
        "start_ts": 100,
        "end_ts": 200,
        "verdict": "outage",
        "decided_at": 1_000,
    }
    defaults.update(overrides)
    return Decision(**defaults)  # type: ignore[arg-type]


def test_add_returns_a_positive_id_and_the_decision_is_active(tmp_path: Path) -> None:
    """Pass-1: a broken `add` (no id returned, or the row not showing up
    in `active()`) would mean a user's confirm/reject click is silently
    lost -- nothing else in the app re-derives a decision from anywhere
    else."""
    store, db, device_id = _env(tmp_path)
    try:
        decision_id = store.add(_decision(device_id))

        assert decision_id > 0
        active = store.active(device_id)
        assert len(active) == 1
        assert active[0].id == decision_id
        assert active[0].verdict == "outage"
        assert active[0].superseded_by is None
    finally:
        db.close()


def test_a_second_decision_on_the_same_target_supersedes_the_first(tmp_path: Path) -> None:
    """Pass-1: without supersession, changing your mind (reject after an
    earlier confirm) would leave BOTH verdicts active, and `active()`
    could not tell a caller which one actually applies."""
    store, db, device_id = _env(tmp_path)
    try:
        first_id = store.add(_decision(device_id, verdict="outage"))
        second_id = store.add(_decision(device_id, verdict="no_outage"))

        active = store.active(device_id)
        assert [d.id for d in active] == [second_id]
        assert active[0].verdict == "no_outage"
        assert first_id != second_id
    finally:
        db.close()


def test_a_decision_on_a_different_target_interval_does_not_supersede(tmp_path: Path) -> None:
    """Pass-1: an over-eager supersession scoped only by device (not by
    target/interval too) would silently erase an unrelated gap's or
    legacy event's decision the moment any other one is recorded."""
    store, db, device_id = _env(tmp_path)
    try:
        first_id = store.add(_decision(device_id, start_ts=100, end_ts=200))
        second_id = store.add(_decision(device_id, start_ts=9_000, end_ts=9_100))

        active_ids = {d.id for d in store.active(device_id)}
        assert active_ids == {first_id, second_id}
    finally:
        db.close()


def test_undo_marks_a_decision_superseded_without_deleting_it(tmp_path: Path) -> None:
    """Pass-1: a user "undo" action that actually deleted the row would
    break the audit trail the design explicitly asks for (superseded_by
    is a self-reference, not a DELETE)."""
    store, db, device_id = _env(tmp_path)
    try:
        decision_id = store.add(_decision(device_id))

        store.undo(decision_id)

        assert store.active(device_id) == []
        row = db.writer.execute(
            "SELECT id, superseded_by FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        assert row is not None  # still there -- not deleted
        assert row["superseded_by"] == decision_id
    finally:
        db.close()
