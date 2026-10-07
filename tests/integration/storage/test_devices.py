"""Integration tests for :class:`DeviceStore`, against a real SQLite file."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore


def _store(tmp_path: Path) -> tuple[DeviceStore, Database]:
    db = Database(tmp_path / "ecoflow-stats.db")
    return DeviceStore(db.writer), db


def test_upserting_a_new_serial_stores_it(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    try:
        record = store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1000)
        assert record.sn == "BA31ZEB1SF7F0001"
        assert record.adapter_id == "delta_pro"
        fetched = store.get_by_sn("BA31ZEB1SF7F0001")
        assert fetched == record
    finally:
        db.close()


def test_upserting_the_same_serial_twice_does_not_duplicate(tmp_path: Path) -> None:
    """Re-running collector startup (which upserts every configured device
    on every boot) must not grow the devices table without bound."""
    store, db = _store(tmp_path)
    try:
        first = store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1000)
        second = store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=2000)
        assert first.id == second.id
        assert len(store.list_all()) == 1
        assert second.adapter_id == "delta_pro"
    finally:
        db.close()


def test_two_devices_are_stored_distinctly(tmp_path: Path) -> None:
    """Storage requirement 'Schema Supports Multiple Devices': two
    configured devices must resolve to two distinct, independently
    addressable rows, not share identity."""
    store, db = _store(tmp_path)
    try:
        a = store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1000)
        b = store.upsert(sn="BA31ZEB1SF7F0002", adapter_id="generic", created_at=1000)
        assert a.id != b.id
        assert {d.sn for d in store.list_all()} == {"BA31ZEB1SF7F0001", "BA31ZEB1SF7F0002"}
    finally:
        db.close()


def test_set_online_updates_the_flag_and_checked_at(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    try:
        record = store.upsert(sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1000)
        store.set_online(record.id, online=True, checked_at=5000)
        updated = store.get_by_sn("BA31ZEB1SF7F0001")
        assert updated is not None
        assert updated.online is True
        assert updated.online_checked_at == 5000
    finally:
        db.close()


def test_unknown_serial_returns_none(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    try:
        assert store.get_by_sn("NOSUCHSERIAL0000") is None
    finally:
        db.close()
