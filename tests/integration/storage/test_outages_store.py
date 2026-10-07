"""Integration tests for `storage.outages.OutageStore` and
`storage.derivations.DerivationStore` against real SQLite.

Storage requirement: derived data is separate from raw samples and
recomputable; outage recomputation is versioned and re-runnable.
"""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.outages.model import Event, Gap
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.outages import OutageStore


def _env(tmp_path: Path) -> tuple[OutageStore, DerivationStore, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    return OutageStore(db.writer), DerivationStore(db.writer), db, device.id


def _event(start_ts: int, **overrides: object) -> Event:
    return Event(start_ts=start_ts, **overrides)  # type: ignore[arg-type]


def _gap(start_ts: int, end_ts: int, **overrides: object) -> Gap:
    defaults: dict[str, object] = {
        "cause": "unknown",
        "state_before": "present",
        "state_after": "present",
        "soc_before": None,
        "soc_after": None,
        "chg_ac_wh_delta": None,
        "expected_in_wh": None,
        "evidence": "inconclusive",
        "failures": {},
    }
    defaults.update(overrides)
    return Gap(start_ts=start_ts, end_ts=end_ts, **defaults)  # type: ignore[arg-type]


def test_replace_from_with_no_start_stores_every_event_and_gap(tmp_path: Path) -> None:
    """A full recompute (`start_ts=None`) must store everything it is
    given -- the baseline a later incremental run is compared against."""
    store, _derivations, db, device_id = _env(tmp_path)
    try:
        events = [_event(0, kind="outage", readings=2, end_ts=120), _event(600, kind="brief")]
        gaps = [_gap(300, 360)]

        store.replace_from(device_id, None, events, gaps)
        db.writer.commit()

        assert store.events(device_id, 0, 1_000) == events
        assert store.gaps(device_id, 0, 1_000) == gaps
    finally:
        db.close()


def test_replace_from_only_touches_rows_at_or_after_start_ts(tmp_path: Path) -> None:
    """An incremental recompute passing a real `start_ts` must leave
    earlier, already-settled events and gaps completely alone -- deleting
    them would silently erase confirmed history on every resume."""
    store, _derivations, db, device_id = _env(tmp_path)
    try:
        early_event = _event(0, kind="outage", readings=2, end_ts=120)
        early_gap = _gap(130, 180)
        store.replace_from(device_id, None, [early_event], [early_gap])
        db.writer.commit()

        new_event = _event(600, kind="brief")
        store.replace_from(device_id, 600, [new_event], [])
        db.writer.commit()

        assert store.events(device_id, 0, 1_000) == [early_event, new_event]
        assert store.gaps(device_id, 0, 1_000) == [early_gap]
    finally:
        db.close()


def test_replace_from_clears_only_rows_in_the_replaced_range(tmp_path: Path) -> None:
    """Re-running `replace_from` at the same `start_ts` with a different
    result must remove the stale row it is replacing, not accumulate
    duplicates -- a recompute is idempotent-by-construction, never additive."""
    store, _derivations, db, device_id = _env(tmp_path)
    try:
        store.replace_from(device_id, 600, [_event(600, kind="brief")], [])
        db.writer.commit()

        store.replace_from(device_id, 600, [_event(600, kind="outage", readings=3, end_ts=780)], [])
        db.writer.commit()

        events = store.events(device_id, 0, 1_000)
        assert len(events) == 1
        assert events[0].kind == "outage"
        assert events[0].readings == 3
    finally:
        db.close()


def test_a_decision_row_is_untouched_by_replace_from(tmp_path: Path) -> None:
    """Decisions are stored entirely separately from derived outage data
    (design D4) and must survive every recompute -- including one whose
    `start_ts` floor covers the decision's own anchor interval -- because
    `replace_from` only ever touches `outage_events` and `gaps`."""
    store, _derivations, db, device_id = _env(tmp_path)
    try:
        db.writer.execute(
            "INSERT INTO decisions (device_id, target, start_ts, end_ts, verdict, decided_at)"
            " VALUES (?, 'gap', 300, 360, 'outage', 1)",
            (device_id,),
        )
        db.writer.commit()

        store.replace_from(device_id, None, [_event(0)], [])
        db.writer.commit()

        (count,) = db.writer.execute(
            "SELECT COUNT(*) FROM decisions WHERE device_id = ? AND verdict = 'outage'",
            (device_id,),
        ).fetchone()
        assert count == 1
    finally:
        db.close()


def test_derivation_get_returns_none_when_never_computed(tmp_path: Path) -> None:
    _store, derivations, db, device_id = _env(tmp_path)
    try:
        assert derivations.get(device_id, "outages") is None
    finally:
        db.close()


def test_mark_computed_then_get_round_trips_every_field(tmp_path: Path) -> None:
    _store, derivations, db, device_id = _env(tmp_path)
    try:
        derivations.mark_computed(
            device_id, "outages", version=1, params_hash="abc", checkpoint_ts=600, computed_at=700
        )
        db.writer.commit()

        row = derivations.get(device_id, "outages")
        assert row is not None
        assert (row.version, row.params_hash, row.checkpoint_ts, row.computed_at) == (
            1,
            "abc",
            600,
            700,
        )
        assert row.dirty_from_ts is None
    finally:
        db.close()


def test_mark_dirty_on_a_fresh_derivation_sets_the_marker(tmp_path: Path) -> None:
    _store, derivations, db, device_id = _env(tmp_path)
    try:
        derivations.mark_dirty(device_id, "outages", 500)

        row = derivations.get(device_id, "outages")
        assert row is not None
        assert row.dirty_from_ts == 500
    finally:
        db.close()


def test_mark_dirty_moves_the_marker_earlier(tmp_path: Path) -> None:
    """A second, earlier dirty mark (an import backfilling older history
    after the collector already marked something newer dirty) must widen
    the dirty range, not narrow it."""
    _store, derivations, db, device_id = _env(tmp_path)
    try:
        derivations.mark_computed(
            device_id, "outages", version=1, params_hash="abc", checkpoint_ts=1000, computed_at=1000
        )
        db.writer.commit()
        derivations.mark_dirty(device_id, "outages", 900)
        derivations.mark_dirty(device_id, "outages", 300)

        row = derivations.get(device_id, "outages")
        assert row is not None
        assert row.dirty_from_ts == 300
    finally:
        db.close()


def test_mark_dirty_never_moves_the_marker_later(tmp_path: Path) -> None:
    """The inverse of the previous case: once dirty from an earlier point,
    a later mark (e.g. the collector's own next tick) must not hide it."""
    _store, derivations, db, device_id = _env(tmp_path)
    try:
        derivations.mark_dirty(device_id, "outages", 300)
        derivations.mark_dirty(device_id, "outages", 900)

        row = derivations.get(device_id, "outages")
        assert row is not None
        assert row.dirty_from_ts == 300
    finally:
        db.close()
