"""Integration tests for `storage.notifications.NotificationLedger`
against real SQLite.

Notifications requirement "No Duplicate Notifications Across Restarts"
(persistence half) and Named Defect "Duplicate alert": `claim()` is the
only de-duplication mechanism (an `INSERT OR IGNORE` on the ledger's own
primary key), so a second claim for the same (device, event start, kind)
must never succeed. `due()` also owns expiring a pending alert that has
been retried for too long (design D10: "retried ... with backoff for up
to 6 hours, then expired").
"""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.notifications.messages import AlertKey
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.notifications import NotificationLedger

_SIX_HOURS_S = 6 * 3600


def _env(tmp_path: Path) -> tuple[NotificationLedger, Database, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    return NotificationLedger(db.writer), db, device.id


def test_claim_stores_a_new_alert_as_pending_and_due(tmp_path: Path) -> None:
    """Pass-1: a broken `claim` that did not actually persist the alert
    would mean a real outage-start transition never actually sends
    anything."""
    ledger, db, device_id = _env(tmp_path)
    try:
        key = AlertKey(device_id=device_id, event_start_ts=1_000, kind="start")

        claimed = ledger.claim(key, "Power is out", "14:30 · battery 97%", now=1_000)

        assert claimed is True
        due = ledger.due(1_000)
        assert len(due) == 1
        assert due[0].key == key
        assert due[0].title == "Power is out"
        assert due[0].attempts == 0
    finally:
        db.close()


def test_a_second_claim_for_the_same_key_is_rejected(tmp_path: Path) -> None:
    """Pass-1: without this guard, replaying the same transition twice
    (for example after a restart) would send a duplicate outage alert
    for the same event -- exactly the Named Defect "Duplicate alert"."""
    ledger, db, device_id = _env(tmp_path)
    try:
        key = AlertKey(device_id=device_id, event_start_ts=1_000, kind="start")
        first = ledger.claim(key, "Power is out", "body one", now=1_000)
        second = ledger.claim(key, "Power is out", "body two", now=1_100)

        assert first is True
        assert second is False
        assert len(ledger.due(1_100)) == 1
    finally:
        db.close()


def test_mark_sent_removes_the_alert_from_due(tmp_path: Path) -> None:
    """Pass-1: a successfully delivered alert that stayed `due` would be
    resent on the very next tick, producing a duplicate the ledger exists
    to prevent."""
    ledger, db, device_id = _env(tmp_path)
    try:
        key = AlertKey(device_id=device_id, event_start_ts=1_000, kind="start")
        ledger.claim(key, "Power is out", "body", now=1_000)

        ledger.mark_sent(key, now=1_005)

        assert ledger.due(1_005) == []
    finally:
        db.close()


def test_mark_failed_increments_attempts_and_keeps_the_alert_due(tmp_path: Path) -> None:
    """Pass-1: a delivery failure that silently dropped the alert would
    violate the at-least-once delivery design (D10) -- a transient ntfy
    outage must still retry later."""
    ledger, db, device_id = _env(tmp_path)
    try:
        key = AlertKey(device_id=device_id, event_start_ts=1_000, kind="start")
        ledger.claim(key, "Power is out", "body", now=1_000)

        ledger.mark_failed(key, "connection refused", now=1_010)

        due = ledger.due(1_010)
        assert len(due) == 1
        assert due[0].attempts == 1
    finally:
        db.close()


def test_an_alert_older_than_six_hours_expires_and_leaves_due(tmp_path: Path) -> None:
    """Pass-1: retrying forever against an unreachable ntfy server would
    never let the ledger settle -- design D10 bounds retries to 6 hours,
    after which the alert is `expired`, not retried indefinitely."""
    ledger, db, device_id = _env(tmp_path)
    try:
        key = AlertKey(device_id=device_id, event_start_ts=1_000, kind="start")
        ledger.claim(key, "Power is out", "body", now=1_000)
        ledger.mark_failed(key, "timeout", now=1_000)

        due = ledger.due(1_000 + _SIX_HOURS_S)

        assert due == []
        row = db.writer.execute(
            "SELECT status FROM notifications WHERE device_id = ? AND event_start_ts = ?",
            (device_id, 1_000),
        ).fetchone()
        assert row["status"] == "expired"
    finally:
        db.close()
