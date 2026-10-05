"""Integration tests for :class:`EcoFlowCloudClient`, against
``httpx.MockTransport`` — no real network call is ever made.

Covers the acquisition "Requests Are Authenticated and Accepted by the Live
API" and "Configurable Regional API Host" requirements, and amendment item
11's named contract: a 401/403 is recorded as an authentication failure,
never treated as an empty or successful reading.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from ecoflow_stats.acquisition.ecoflow_client import (
    CloudApiError,
    CloudBadPayload,
    CloudHttpError,
    CloudNetworkError,
    CloudTimeout,
    DeviceInfo,
    EcoFlowCloudClient,
)

_ACCESS_KEY = "AKIDEXAMPLE1234"
_SECRET_KEY = "s3cr3t-signing-key"


class _CountingHandler:
    """Wraps a response-building function and counts how many times it ran."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self._respond = respond
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._respond(request)


def _client(
    handler: _CountingHandler, base_url: str = "https://api.ecoflow.com"
) -> EcoFlowCloudClient:
    return EcoFlowCloudClient(
        base_url=base_url,
        access_key=_ACCESS_KEY,
        secret_key=_SECRET_KEY,
        transport=httpx.MockTransport(handler),
    )


def _ok(body: object) -> httpx.Response:
    return httpx.Response(200, json=body)


@pytest.mark.anyio
async def test_list_devices_returns_parsed_device_info() -> None:
    """A successful device-list response is parsed into DeviceInfo values."""
    handler = _CountingHandler(
        lambda _request: _ok(
            {
                "code": "0",
                "message": "success",
                "data": [
                    {
                        "sn": "BA31ZEB1SF7F0001",
                        "deviceName": "Garage",
                        "productName": "DELTA Pro",
                        "online": 1,
                    },
                ],
            }
        )
    )
    devices = await _client(handler).list_devices()
    assert devices == [
        DeviceInfo(sn="BA31ZEB1SF7F0001", name="Garage", product_name="DELTA Pro", online=True)
    ]


@pytest.mark.anyio
async def test_fetch_quota_returns_the_raw_data_mapping() -> None:
    """A successful quota response's 'data' object is returned as-is, keyed
    exactly as the API returns it (no field renaming at this layer)."""
    handler = _CountingHandler(
        lambda _request: _ok({"code": 0, "message": "success", "data": {"inv.acInVol": 115000}})
    )
    quota = await _client(handler).fetch_quota("BA31ZEB1SF7F0001")
    assert quota == {"inv.acInVol": 115000}


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_unauthorized_responses_raise_cloud_http_error_not_empty_data(status: int) -> None:
    """Amendment item 11: a 401/403 is an authentication failure, never an
    empty or successful reading. Asserting the raised status, rather than a
    returned empty list, is exactly what this contract requires."""
    handler = _CountingHandler(lambda _request: httpx.Response(status))
    with pytest.raises(CloudHttpError) as exc_info:
        await _client(handler).list_devices()
    assert exc_info.value.status == status


@pytest.mark.anyio
async def test_timeout_raises_cloud_timeout_and_is_retried() -> None:
    """A connect/read timeout is retried once, then raised as CloudTimeout."""

    def _raise_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    handler = _CountingHandler(_raise_timeout)
    with pytest.raises(CloudTimeout):
        await _client(handler).list_devices()
    assert handler.calls == 2


@pytest.mark.anyio
async def test_connection_error_raises_cloud_network_error() -> None:
    """A DNS/connection failure is reported as CloudNetworkError, distinct
    from a timeout — the two are different failure categories to an owner
    diagnosing a collection outage."""

    def _raise_connect_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed")

    handler = _CountingHandler(_raise_connect_error)
    with pytest.raises(CloudNetworkError):
        await _client(handler).list_devices()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [500, 503, 429])
async def test_server_errors_and_rate_limit_are_retried_then_raised(status: int) -> None:
    """5xx and 429 are transient — retried once before being raised."""
    handler = _CountingHandler(lambda _request: httpx.Response(status))
    with pytest.raises(CloudHttpError) as exc_info:
        await _client(handler).list_devices()
    assert exc_info.value.status == status
    assert handler.calls == 2


@pytest.mark.anyio
async def test_other_4xx_is_raised_without_retry() -> None:
    """A 404 (for example) is a client-side problem, not a transient fault —
    retrying it would waste the retry budget on a request that can never
    succeed unmodified."""
    handler = _CountingHandler(lambda _request: httpx.Response(404))
    with pytest.raises(CloudHttpError) as exc_info:
        await _client(handler).list_devices()
    assert exc_info.value.status == 404
    assert handler.calls == 1


@pytest.mark.anyio
async def test_non_json_body_raises_cloud_bad_payload_without_retry() -> None:
    """A response that is not JSON at all is a bad payload, not a transient
    fault — it would fail identically on retry."""
    handler = _CountingHandler(lambda _request: httpx.Response(200, text="not json"))
    with pytest.raises(CloudBadPayload):
        await _client(handler).list_devices()
    assert handler.calls == 1


@pytest.mark.anyio
async def test_api_error_code_raises_cloud_api_error_without_retry() -> None:
    """A non-zero API 'code' in an otherwise-200 response is the API
    reporting its own failure (for example, an unknown serial) — not a
    transient fault, so it is not retried."""
    handler = _CountingHandler(
        lambda _request: _ok({"code": "8521", "message": "signature is wrong", "data": None})
    )
    with pytest.raises(CloudApiError) as exc_info:
        await _client(handler).list_devices()
    assert exc_info.value.code == "8521"
    assert handler.calls == 1


@pytest.mark.anyio
async def test_request_targets_the_configured_host() -> None:
    """The request's URL reflects whatever base_url was configured."""
    seen_hosts: list[str] = []

    def _capture_host(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return _ok({"code": "0", "message": "ok", "data": []})

    handler = _CountingHandler(_capture_host)
    await _client(handler, base_url="https://api.ecoflow.com").list_devices()
    assert seen_hosts == ["api.ecoflow.com"]


@pytest.mark.anyio
async def test_changing_the_configured_host_changes_the_request_target() -> None:
    """The same client code, pointed at a different configured host, sends
    its request to that different host — with no code change."""
    seen_hosts: list[str] = []

    def _capture_host(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return _ok({"code": "0", "message": "ok", "data": []})

    handler = _CountingHandler(_capture_host)
    await _client(handler, base_url="https://api-e.ecoflow.com").list_devices()
    assert seen_hosts == ["api-e.ecoflow.com"]
