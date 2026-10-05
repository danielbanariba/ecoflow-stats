"""Integration tests for :class:`LegacyStore` (the `legacy_outages` table)."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.imports import ImportRunStore
from ecoflow_stats.storage.legacy import LegacyStore


def _store(tmp_path: Path) -> tuple[LegacyStore, Database, int, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    import_id = ImportRunStore(db.writer).start(
        device.id, started_at=1, source_tz="America/Tegucigalpa"
    )
    return LegacyStore(db.writer), db, device.id, import_id


def test_inserting_the_same_event_twice_does_not_duplicate(tmp_path: Path) -> None:
    """A re-run of the import over an unchanged source must not multiply a
    stored event (history-import: idempotent and re-runnable)."""
    store, db, device_id, import_id = _store(tmp_path)
    try:
        kwargs = {
            "device_id": device_id,
            "start_ts": 1_000,
            "end_ts": 1_060,
            "soc_start": 90,
            "soc_end": 85,
            "logged_minutes": 1,
            "start_line": "L1",
            "end_line": "L2",
            "source_tz": "America/Tegucigalpa",
            "flags": frozenset(),
            "import_id": import_id,
        }
        store.upsert(**kwargs)
        store.upsert(**kwargs)
        (count,) = db.writer.execute("SELECT COUNT(*) FROM legacy_outages").fetchone()
        assert count == 1
    finally:
        db.close()


def test_reimporting_closes_a_previously_open_event(tmp_path: Path) -> None:
    """A `corte` imported before its `retorno` existed (open, flagged
    `orphan_start`) must be closed — not duplicated — once a later import
    reads a log that by then also has the matching `retorno`."""
    store, db, device_id, import_id = _store(tmp_path)
    try:
        store.upsert(
            device_id=device_id,
            start_ts=1_000,
            end_ts=None,
            soc_start=90,
            soc_end=None,
            logged_minutes=None,
            start_line="L1",
            end_line=None,
            source_tz="America/Tegucigalpa",
            flags=frozenset({"orphan_start"}),
            import_id=import_id,
        )
        second_import_id = ImportRunStore(db.writer).start(
            device_id, started_at=2, source_tz="America/Tegucigalpa"
        )
        store.upsert(
            device_id=device_id,
            start_ts=1_000,
            end_ts=1_200,
            soc_start=90,
            soc_end=80,
            logged_minutes=3,
            start_line="L1",
            end_line="L2",
            source_tz="America/Tegucigalpa",
            flags=frozenset(),
            import_id=second_import_id,
        )
        row = store.get(device_id, 1_000)
        assert row is not None
        assert row.end_ts == 1_200
        assert row.soc_end == 80
        assert row.logged_minutes == 3
        (count,) = db.writer.execute("SELECT COUNT(*) FROM legacy_outages").fetchone()
        assert count == 1
    finally:
        db.close()


def test_reimporting_an_already_closed_event_changes_nothing(tmp_path: Path) -> None:
    """An event that is already closed must not be reopened or altered by
    a later import re-reading the same source line — the only allowed
    update closes an open event, nothing else."""
    store, db, device_id, import_id = _store(tmp_path)
    try:
        store.upsert(
            device_id=device_id,
            start_ts=1_000,
            end_ts=1_200,
            soc_start=90,
            soc_end=80,
            logged_minutes=3,
            start_line="L1",
            end_line="L2",
            source_tz="America/Tegucigalpa",
            flags=frozenset(),
            import_id=import_id,
        )
        store.upsert(
            device_id=device_id,
            start_ts=1_000,
            end_ts=9_999,
            soc_start=1,
            soc_end=1,
            logged_minutes=999,
            start_line="L1",
            end_line="BOGUS",
            source_tz="America/Tegucigalpa",
            flags=frozenset({"suspected_phantom"}),
            import_id=import_id,
        )
        row = store.get(device_id, 1_000)
        assert row is not None
        assert row.end_ts == 1_200
        assert row.soc_end == 80
        assert row.logged_minutes == 3
    finally:
        db.close()
