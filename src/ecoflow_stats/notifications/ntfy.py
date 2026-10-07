"""The ntfy HTTP adapter: `NtfyNotifier` POSTs one message per `send()`
call to a configured ntfy topic, matching the legacy watcher's own
headers convention (design-edges section 3) -- `Title`, `Priority`
(`high` for a start alert, `default` for an end alert), `Tags`, and an
optional bearer token. Only ever constructed when
`ECOFLOW_STATS_NTFY_TOPIC` is configured (notifications requirement:
"Notifications Are Opt-In").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ecoflow_stats.notifications.messages import Message

_TIMEOUT_S = 10.0
_PRIORITY = {"start": "high", "end": "default"}
_TAGS = {"start": "electric_plug,warning", "end": "electric_plug,white_check_mark"}


class NotifyError(Exception):
    """Raised when delivering a notification fails: a network error, a
    timeout, or a non-2xx response from the ntfy server. Every
    `Notifier` implementation (real or test fake) raises this same type,
    so `notifications.service` can isolate a delivery failure without
    depending on which adapter produced it."""


class NtfyNotifier:
    """Sends one `Message` per `send()` call to a configured ntfy topic."""

    def __init__(
        self,
        *,
        base_url: str,
        topic: str,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._topic = topic
        self._token = token
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=_TIMEOUT_S)

    async def send(self, message: Message) -> None:
        headers = {
            "Title": message.title,
            "Priority": _PRIORITY.get(message.kind, "default"),
            "Tags": _TAGS.get(message.kind, "electric_plug"),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._http.post(
                f"/{self._topic}", content=message.body.encode(), headers=headers
            )
        except httpx.TimeoutException as exc:
            raise NotifyError(f"ntfy request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise NotifyError(f"ntfy request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NotifyError(f"ntfy returned HTTP {response.status_code}")


__all__ = ["NotifyError", "NtfyNotifier"]
