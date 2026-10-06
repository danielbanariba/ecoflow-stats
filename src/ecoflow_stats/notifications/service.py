"""Wires one live `Started`/`Ended` transition through to an actual
delivery attempt, and owns the periodic retry pass over the ledger's own
`due()` queue (design D10: "retried each tick with backoff for up to 6
hours, then expired"). Covers notifications requirements "Alert on the
First Below-Threshold Reading", "Outage-End Notification Includes
Duration and Charge", "No Duplicate Notifications Across Restarts", and
"Notification Failures Are Isolated From Collection".

The collector is the only caller that constructs and uses this, and only
when `ECOFLOW_STATS_NTFY_TOPIC` is configured (notifications requirement:
"Notifications Are Opt-In") -- import and recompute never touch it at
all, which is what keeps those paths silent by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ecoflow_stats.notifications.messages import AlertKey, Message, end_message, start_message
from ecoflow_stats.notifications.ntfy import NotifyError
from ecoflow_stats.outages.model import Started

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from ecoflow_stats.notifications.messages import Kind, Lang
    from ecoflow_stats.outages.model import Ended
    from ecoflow_stats.ports import Clock, NotificationLedger, Notifier

_LATE_SUFFIX = " (sent late)"


@dataclass
class NotificationService:
    """One device-set's notification wiring: a shared ledger and
    notifier, the configured language, and an optional device-label map
    (used only when more than one device is configured)."""

    ledger: NotificationLedger
    notifier: Notifier
    clock: Clock
    lang: Lang
    device_labels: Mapping[int, str] = field(default_factory=dict)

    async def handle_transition(self, device_id: int, transition: Started | Ended) -> None:
        """Render, claim, and attempt delivery for one live transition.
        Never raises: a notifier failure is isolated here, recorded in
        the ledger for `retry_due` to pick up later."""
        kind: Kind = "start" if isinstance(transition, Started) else "end"
        event = transition.event
        now = self.clock.now()
        label = self.device_labels.get(device_id) if len(self.device_labels) > 1 else None
        message = (
            start_message(lang=self.lang, event=event, now=now, device_label=label)
            if kind == "start"
            else end_message(lang=self.lang, event=event, now=now, device_label=label)
        )
        key = AlertKey(device_id=device_id, event_start_ts=event.start_ts, kind=kind)
        now_ts = int(now.timestamp())
        if not self.ledger.claim(key, message.title, message.body, now_ts):
            return  # already claimed by an earlier live transition or a replay of one
        await self._deliver(key, message.title, message.body, now_ts)

    async def retry_due(self, now: datetime) -> None:
        """Re-attempt every alert the ledger still has pending -- design
        D10's bounded retry, called once per collector tick."""
        now_ts = int(now.timestamp())
        for pending in self.ledger.due(now_ts):
            body = pending.body if pending.attempts == 0 else f"{pending.body}{_LATE_SUFFIX}"
            await self._deliver(pending.key, pending.title, body, now_ts)

    async def _deliver(self, key: AlertKey, title: str, body: str, now_ts: int) -> None:
        try:
            await self.notifier.send(Message(title=title, body=body, kind=key.kind))
        except NotifyError as exc:
            self.ledger.mark_failed(key, str(exc), now_ts)
        else:
            self.ledger.mark_sent(key, now_ts)


__all__ = ["NotificationService"]
