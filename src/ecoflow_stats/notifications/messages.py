"""Bilingual notification message formatting and the small value types the
notification ledger and service exchange (notifications requirements:
"Outage-End Notification Includes Duration and Charge", "Notification
Message Language Is Configurable").

Pure: given an already-closed (or just-opened) `Event` plus the moment the
message is built, produces the exact title/body text -- no I/O, no ledger,
no HTTP. Wording mirrors the legacy watcher's own style (design-edges
section 3): `HH:MM · battery NN% · <clause>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ecoflow_stats.outages.model import Event

Lang = Literal["en", "es"]
Kind = Literal["start", "end"]


@dataclass(frozen=True, slots=True)
class AlertKey:
    """Identifies one alert slot in the notification ledger: at most one
    (device, event, kind) triple is ever claimed, which is what makes a
    restart replay idempotent (notifications requirement: "No Duplicate
    Notifications Across Restarts")."""

    device_id: int
    event_start_ts: int
    kind: Kind


@dataclass(frozen=True, slots=True)
class Message:
    """One rendered notification, ready to hand to a `Notifier`."""

    title: str
    body: str
    kind: Kind | None = None
    """`None` only for a `Message` built outside `start_message`/
    `end_message` (for example, a test constructing one directly); the
    `ntfy` adapter uses it to pick the matching Priority/Tags (design-edges
    section 3: start is `high` priority, end is `default`)."""


@dataclass(frozen=True, slots=True)
class PendingAlert:
    """One not-yet-successfully-delivered alert due for a (re)send
    attempt, as `NotificationLedger.due` returns it."""

    key: AlertKey
    title: str
    body: str
    attempts: int


def _format_duration(seconds: int) -> str:
    """`"8 h 32 min"`, or `"12 min"` under an hour -- the watcher's own
    wording style, never fractional."""
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def _titled(title: str, device_label: str | None) -> str:
    return f"{device_label}: {title}" if device_label else title


def start_message(
    *, lang: Lang, event: Event, now: datetime, device_label: str | None = None
) -> Message:
    """The outage-start alert: sent on the very first below-threshold
    live reading, before the 2-reading debounce confirms an official
    outage (notifications requirement: "Alert on the First
    Below-Threshold Reading")."""
    title = _titled("Power is out" if lang == "en" else "Se fue la luz", device_label)
    parts = [f"{now:%H:%M}"]
    if event.soc_start is not None:
        label = "battery" if lang == "en" else "batería"
        parts.append(f"{label} {event.soc_start}%")
    if event.dsg_remain_min_start is not None:
        remaining = _format_duration(event.dsg_remain_min_start * 60)
        parts.append(f"about {remaining} left" if lang == "en" else f"quedan unos {remaining}")
    return Message(title=title, body=" · ".join(parts), kind="start")


def end_message(
    *, lang: Lang, event: Event, now: datetime, device_label: str | None = None
) -> Message:
    """The outage-end alert, sent on the first above-threshold reading
    after a start (notifications requirement: "Outage-End Notification
    Includes Duration and Charge"). `event.end_ts` must already be set
    -- only ever called from an `Ended` transition's event."""
    assert event.end_ts is not None
    title = _titled("Power is back" if lang == "en" else "Volvió la luz", device_label)
    parts = [f"{now:%H:%M}"]
    if event.soc_end is not None:
        label = "battery" if lang == "en" else "batería"
        parts.append(f"{label} {event.soc_end}%")

    if event.end_in_gap and event.end_uncertainty_s is not None:
        window_start = datetime.fromtimestamp(event.end_ts - event.end_uncertainty_s, tz=now.tzinfo)
        window_end = datetime.fromtimestamp(event.end_ts, tz=now.tzinfo)
        clause = (
            f"returned between {window_start:%H:%M} and {window_end:%H:%M}"
            if lang == "en"
            else f"volvió entre las {window_start:%H:%M} y las {window_end:%H:%M}"
        )
    elif event.kind == "brief":
        clause = "lasted under 2 min" if lang == "en" else "duró menos de 2 min"
    else:
        duration = _format_duration(event.end_ts - event.start_ts)
        clause = f"lasted {duration}" if lang == "en" else f"duró {duration}"
    parts.append(clause)
    return Message(title=title, body=" · ".join(parts), kind="end")


__all__ = ["AlertKey", "Kind", "Lang", "Message", "PendingAlert", "end_message", "start_message"]
