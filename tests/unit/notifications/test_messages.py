"""Unit tests for bilingual notification message formatting (pure).

Notifications requirements: "Outage-End Notification Includes Duration
and Charge", "Notification Message Language Is Configurable". Design-edges
section 3 fixes the literal wording this mirrors (watcher parity).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ecoflow_stats.notifications.messages import end_message, start_message
from ecoflow_stats.outages.model import Event


def _event(**overrides: object) -> Event:
    defaults: dict[str, object] = {"start_ts": 1_000}
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


def test_start_message_renders_in_the_configured_language() -> None:
    """Pass-1: hardcoding English would mean a Spanish-configured
    household gets an alert it cannot read (scenario "The configured
    language controls the message text")."""
    event = _event(soc_start=97)

    en = start_message(lang="en", event=event, now=datetime(2026, 1, 1, 14, 30, tzinfo=UTC))
    es = start_message(lang="es", event=event, now=datetime(2026, 1, 1, 14, 30, tzinfo=UTC))

    assert en.title == "Power is out"
    assert es.title == "Se fue la luz"
    assert "97%" in en.body
    assert "97%" in es.body
    assert "14:30" in en.body
    assert en.kind == "start"


def test_end_message_includes_duration_and_end_of_outage_charge() -> None:
    """Pass-1: an end alert without duration/charge would fail the
    notifications requirement "Outage-End Notification Includes
    Duration and Charge" outright (scenario "The end notification
    carries duration and charge")."""
    event = _event(
        start_ts=1_000,
        end_ts=1_000 + 8 * 3600 + 32 * 60,
        soc_end=42,
        kind="outage",
        readings=2,
    )

    message = end_message(lang="en", event=event, now=datetime(2026, 1, 1, 22, 32, tzinfo=UTC))

    assert "42%" in message.body
    assert "8 h 32 min" in message.body
    assert message.kind == "end"


def test_a_brief_drops_end_message_says_lasted_under_two_minutes() -> None:
    """Pass-1: reporting an exact tiny duration for a brief drop would
    claim a precision the single-reading debounce cannot actually
    support."""
    event = _event(start_ts=1_000, end_ts=1_060, kind="brief", readings=1, soc_end=80)

    message = end_message(lang="en", event=event, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert "lasted under 2 min" in message.body


def test_an_end_after_a_gap_message_reports_a_return_window_not_a_false_exact_time() -> None:
    """Pass-1: stating an exact return time the detector cannot actually
    know (the readings were missing during the gap) would misrepresent
    the detector's own uncertainty as false precision."""
    event = _event(
        start_ts=1_000,
        end_ts=2_000,
        end_in_gap=True,
        end_uncertainty_s=300,
        kind="outage",
        readings=2,
    )

    message = end_message(lang="en", event=event, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert "returned between" in message.body


def test_messages_are_device_label_prefixed_when_a_label_is_given() -> None:
    """Pass-1: with more than one device configured, an un-prefixed
    alert leaves the household guessing which unit lost power."""
    event = _event(soc_start=50)

    message = start_message(
        lang="en", event=event, now=datetime(2026, 1, 1, tzinfo=UTC), device_label="…0254"
    )

    assert message.title == "…0254: Power is out"


def test_messages_are_not_prefixed_when_no_label_is_given() -> None:
    """Pass-1: a single-device household (the common case) must not see
    a redundant device prefix on every alert."""
    event = _event(soc_start=50)

    message = start_message(lang="en", event=event, now=datetime(2026, 1, 1, tzinfo=UTC))

    assert message.title == "Power is out"
