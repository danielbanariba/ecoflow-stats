"""Integration tests for `notifications.service.NotificationService`,
against a `RecordingNotifier` fake and a real SQLite-backed
`NotificationLedger` — no real network call is ever made.

Notifications requirements: "A configured topic receives the
notification", "No Duplicate Notifications Across Restarts",
"Notification Failures Are Isolated From Collection"; design D10's
at-least-once retry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.notifications.service import NotificationService
from ecoflow_stats.outages.model import Ended, Event, Started
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.notifications import NotificationLedger
from tests.fakes import FakeClock, RecordingNotifier


def _env(tmp_path: Path) -> tuple[Database, NotificationLedger, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="generic", created_at=1
    )
    return db, NotificationLedger(db.writer), device.id


def _event(**overrides: object) -> Event:
    defaults: dict[str, object] = {"start_ts": 1_000, "soc_start": 90}
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_a_configured_topic_receives_the_start_notification(tmp_path: Path) -> None:
    """Pass-1: a service that built the message but never actually
    called the notifier would leave the operator with no alert despite
    a correctly configured topic (scenario "A configured topic receives
    the notification")."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier()
        service = NotificationService(
            ledger=ledger,
            notifier=notifier,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
            lang="en",
        )

        await service.handle_transition(device_id, Started(_event()))

        assert len(notifier.sent) == 1
        assert notifier.sent[0].title == "Power is out"
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_second_handling_of_the_same_event_sends_no_duplicate(tmp_path: Path) -> None:
    """Pass-1: without the ledger's own claim-based dedupe, replaying
    the same `Started` transition after a restart (the machine rebuilt
    by replay) would send the outage-start alert twice for the same
    real event (scenario "A restart mid-outage sends no second start
    alert")."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier()
        service = NotificationService(
            ledger=ledger,
            notifier=notifier,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
            lang="en",
        )
        event = _event()

        await service.handle_transition(device_id, Started(event))
        await service.handle_transition(device_id, Started(event))

        assert len(notifier.sent) == 1
    finally:
        db.close()


@pytest.mark.anyio
async def test_an_unreachable_notifier_does_not_raise_and_leaves_the_alert_pending(
    tmp_path: Path,
) -> None:
    """Pass-1: letting a notifier failure escape `handle_transition`
    would mean an unreachable ntfy server crashes the collector's own
    tick instead of being isolated (notifications requirement:
    "Notification Failures Are Isolated From Collection")."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier(fail_times=99)
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        service = NotificationService(ledger=ledger, notifier=notifier, clock=clock, lang="en")

        await service.handle_transition(device_id, Started(_event()))  # must not raise

        due = ledger.due(int(clock.now().timestamp()))
        assert len(due) == 1
        assert due[0].attempts == 1
    finally:
        db.close()


@pytest.mark.anyio
async def test_retry_due_resends_a_previously_failed_alert_once_it_succeeds(
    tmp_path: Path,
) -> None:
    """Pass-1: a ledger that recorded a failed attempt but was never
    retried would leave a real outage alert undelivered forever once
    the first send happened to fail -- design D10's at-least-once
    delivery depends on this retry pass actually running."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier(fail_times=1)
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        service = NotificationService(ledger=ledger, notifier=notifier, clock=clock, lang="en")

        await service.handle_transition(device_id, Started(_event()))  # first attempt fails
        await service.retry_due(clock.now())  # second attempt succeeds

        assert len(notifier.sent) == 1
        assert ledger.due(int(clock.now().timestamp())) == []
    finally:
        db.close()


@pytest.mark.anyio
async def test_an_end_transition_sends_its_own_alert_independent_of_the_start(
    tmp_path: Path,
) -> None:
    """Pass-1: sharing one ledger key between a start and end alert for
    the same event would mean the end notification is silently rejected
    as a duplicate of the start, instead of being its own delivery."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier()
        service = NotificationService(
            ledger=ledger,
            notifier=notifier,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
            lang="en",
        )
        event = _event(end_ts=1_060, soc_end=85, kind="brief", readings=1)

        await service.handle_transition(device_id, Started(event))
        await service.handle_transition(device_id, Ended(event))

        assert len(notifier.sent) == 2
        assert notifier.sent[1].title == "Power is back"
    finally:
        db.close()


@pytest.mark.anyio
async def test_a_message_is_device_label_prefixed_when_more_than_one_device_is_configured(
    tmp_path: Path,
) -> None:
    """Pass-1: without forwarding `device_labels` into the rendered
    message, a two-device household could not tell which unit an alert
    is about."""
    db, ledger, device_id = _env(tmp_path)
    try:
        notifier = RecordingNotifier()
        service = NotificationService(
            ledger=ledger,
            notifier=notifier,
            clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)),
            lang="en",
            device_labels={device_id: "…0254", 999: "…0001"},
        )

        await service.handle_transition(device_id, Started(_event()))

        assert notifier.sent[0].title == "…0254: Power is out"
    finally:
        db.close()
