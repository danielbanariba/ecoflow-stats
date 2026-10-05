"""Read-only EcoFlow cloud HTTP client.

``EcoFlowCloudClient`` exposes exactly two public coroutines —
``list_devices`` and ``fetch_quota`` — each a GET to one of the two
allowlisted paths in :data:`ALLOWED_PATHS`. Every request is routed through
:class:`ReadOnlyTransport`, which rejects anything else (wrong method, wrong
path) before any I/O happens: there is no code path anywhere in this class
that can reach a control, configuration, or MQTT request. No other module in
this codebase names an EcoFlow endpoint path (contract-tested).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from ecoflow_stats.acquisition.signing import sign

if TYPE_CHECKING:
    from collections.abc import Mapping

ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "/iot-open/sign/device/list",
        "/iot-open/sign/device/quota/all",
    }
)

_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_S = 0.01
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class NonAllowlistedRequest(Exception):
    """Raised before any I/O when a request is not an allowlisted GET."""


class CloudError(Exception):
    """Base class for every EcoFlow cloud client failure."""


class CloudTimeout(CloudError):
    """A connect or read timeout."""


class CloudNetworkError(CloudError):
    """A DNS failure or a connection that could not be established."""


class CloudHttpError(CloudError):
    """The API responded with an HTTP error status."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"EcoFlow cloud returned HTTP {status}")


class CloudApiError(CloudError):
    """The API responded 200 but its own ``code`` field reports a failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"EcoFlow cloud API error {code}: {message}")


class CloudBadPayload(CloudError):
    """The response body was not JSON, or its ``data`` field had the wrong shape."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One device as returned by the account's device list."""

    sn: str
    name: str | None
    product_name: str | None
    online: bool | None


class ReadOnlyTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport, rejecting anything but an allowlisted GET.

    The rejection happens before ``inner`` is ever touched, so a request
    that is not one of the two allowlisted reads never reaches the network.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path not in ALLOWED_PATHS:
            raise NonAllowlistedRequest(f"{request.method} {request.url.path} is not allowlisted")
        return await self._inner.handle_async_request(request)


def _parse_envelope(response: httpx.Response) -> Mapping[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        raise CloudBadPayload("response body is not JSON") from exc
    if not isinstance(body, dict):
        raise CloudBadPayload("response body is not a JSON object")
    code = body.get("code")
    if code not in ("0", 0):
        raise CloudApiError(str(code), str(body.get("message", "")))
    return body


def _parse_device_info(item: Mapping[str, object]) -> DeviceInfo:
    online_raw = item.get("online")
    return DeviceInfo(
        sn=str(item.get("sn", "")),
        name=item.get("deviceName") if isinstance(item.get("deviceName"), str) else None,
        product_name=item.get("productName") if isinstance(item.get("productName"), str) else None,
        online=bool(online_raw) if isinstance(online_raw, int) else None,
    )


class EcoFlowCloudClient:
    """Read-only EcoFlow cloud client: exactly two GET requests.

    ``transport`` is the underlying (real or test-double) transport; it is
    always wrapped in :class:`ReadOnlyTransport`, so even a caller that
    somehow obtained a reference to the internal ``httpx.AsyncClient``
    still cannot issue anything but the two allowlisted GETs.
    """

    def __init__(
        self,
        base_url: str,
        access_key: str,
        secret_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        inner = transport if transport is not None else httpx.AsyncHTTPTransport()
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=ReadOnlyTransport(inner),
            timeout=_HTTP_TIMEOUT,
        )

    async def list_devices(self) -> list[DeviceInfo]:
        """Return the configured account's devices."""
        body = await self._get("/iot-open/sign/device/list")
        data = body.get("data")
        if not isinstance(data, list):
            raise CloudBadPayload("device list response 'data' is not a list")
        return [_parse_device_info(item) for item in data if isinstance(item, dict)]

    async def fetch_quota(self, sn: str) -> Mapping[str, object]:
        """Return one device's full raw quota payload, keyed as the API returns it."""
        body = await self._get("/iot-open/sign/device/quota/all", params={"sn": sn})
        data = body.get("data")
        if not isinstance(data, dict):
            raise CloudBadPayload("quota response 'data' is not an object")
        return data

    async def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> Mapping[str, object]:
        last_error: CloudError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._send(path, params)
            except httpx.TimeoutException as exc:
                last_error = CloudTimeout(str(exc))
            except httpx.RequestError as exc:
                last_error = CloudNetworkError(str(exc))
            else:
                status = response.status_code
                if status == 429 or status >= 500:
                    last_error = CloudHttpError(status)
                elif status >= 400:
                    raise CloudHttpError(status)
                else:
                    return _parse_envelope(response)
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_S)
        assert last_error is not None  # the loop above always sets it before exiting
        raise last_error

    async def _send(self, path: str, params: Mapping[str, str] | None) -> httpx.Response:
        nonce = str(secrets.randbelow(900_000) + 100_000)
        timestamp_ms = str(int(time.time() * 1000))
        headers = {
            "accessKey": self._access_key,
            "nonce": nonce,
            "timestamp": timestamp_ms,
            "sign": sign(
                secret_key=self._secret_key,
                access_key=self._access_key,
                nonce=nonce,
                timestamp_ms=timestamp_ms,
            ),
            "Content-Type": "application/json",
        }
        return await self._http.get(path, params=params, headers=headers)


__all__ = [
    "ALLOWED_PATHS",
    "CloudApiError",
    "CloudBadPayload",
    "CloudError",
    "CloudHttpError",
    "CloudNetworkError",
    "CloudTimeout",
    "DeviceInfo",
    "EcoFlowCloudClient",
    "NonAllowlistedRequest",
    "ReadOnlyTransport",
]
