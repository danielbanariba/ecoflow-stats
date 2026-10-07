"""Integration tests for `notifications.ntfy.NtfyNotifier`, against
``httpx.MockTransport`` — no real network call is ever made.

Design-edges section 3: one POST to ``{base_url}/{topic}`` per message,
with ``Title``/``Priority``/``Tags`` headers (start ``high`` priority and
``warning`` tag, end ``default`` priority and ``white_check_mark`` tag, as
the legacy watcher used) and an optional bearer token.
"""

from __future__ import annotations

import httpx
import pytest

from ecoflow_stats.notifications.messages import Message
from ecoflow_stats.notifications.ntfy import NotifyError, NtfyNotifier


def _notifier(handler, *, token: str | None = None) -> NtfyNotifier:
    return NtfyNotifier(
        base_url="https://ntfy.sh",
        topic="my-topic",
        token=token,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.anyio
async def test_send_posts_to_the_configured_topic_with_title_and_body() -> None:
    """Pass-1: a wrong path or missing title header would mean the
    operator's phone shows an alert with no readable subject, or no
    alert at all if the ntfy server rejects the request outright."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["title"] = request.headers.get("Title")
        captured["body"] = request.content.decode()
        return httpx.Response(200)

    notifier = _notifier(handler)
    await notifier.send(Message(title="Power is out", body="14:30 · battery 97%", kind="start"))

    assert captured["path"] == "/my-topic"
    assert captured["title"] == "Power is out"
    assert captured["body"] == "14:30 · battery 97%"


@pytest.mark.anyio
async def test_a_start_alert_is_sent_at_high_priority_and_an_end_alert_at_default() -> None:
    """Pass-1: swapping priorities would bury a real outage-start alert
    at the same priority as routine status notifications."""
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Priority"))
        return httpx.Response(200)

    notifier = _notifier(handler)
    await notifier.send(Message(title="t", body="b", kind="start"))
    await notifier.send(Message(title="t", body="b", kind="end"))

    assert captured == ["high", "default"]


@pytest.mark.anyio
async def test_send_includes_a_bearer_token_only_when_one_is_configured() -> None:
    """Pass-1: always sending an empty/placeholder Authorization header
    would either leak a malformed credential or break a private ntfy
    instance that actually requires one."""
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization"))
        return httpx.Response(200)

    with_token = _notifier(handler, token="s3cr3t")
    without_token = _notifier(handler)
    await with_token.send(Message(title="t", body="b", kind="start"))
    await without_token.send(Message(title="t", body="b", kind="start"))

    assert captured == ["Bearer s3cr3t", None]


@pytest.mark.anyio
async def test_a_non_2xx_response_raises_notify_error() -> None:
    """Pass-1: swallowing a 4xx/5xx response as success would mean the
    notification ledger marks a never-delivered alert `sent`, losing it
    permanently (the opposite of the at-least-once design, D10)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    notifier = _notifier(handler)

    with pytest.raises(NotifyError):
        await notifier.send(Message(title="t", body="b", kind="start"))


@pytest.mark.anyio
async def test_a_network_error_raises_notify_error_not_an_unhandled_exception() -> None:
    """Pass-1: letting a raw `httpx` exception escape would mean an
    unreachable ntfy server crashes the collector's own tick instead of
    being isolated (notifications requirement: "Notification Failures
    Are Isolated From Collection")."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    notifier = _notifier(handler)

    with pytest.raises(NotifyError):
        await notifier.send(Message(title="t", body="b", kind="start"))
