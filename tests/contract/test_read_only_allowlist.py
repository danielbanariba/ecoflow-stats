"""Contract tests: the acquisition layer can issue only the two allowlisted
read-only GET requests, and no other module depends on an EcoFlow endpoint
path or an MQTT library.

These guard the acquisition "Read-Only Request Allowlist" requirement and
the "Non-allowlisted request" / "Client surface" named defects: a future
change that adds a third endpoint, a control request, or scatters endpoint
paths across the codebase breaks one of these tests.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import httpx
import pytest

from ecoflow_stats.acquisition.ecoflow_client import (
    ALLOWED_PATHS,
    EcoFlowCloudClient,
    NonAllowlistedRequest,
    ReadOnlyTransport,
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


class _RecordingInnerTransport(httpx.AsyncBaseTransport):
    """Inner transport double that records whether it was ever reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json={"code": "0", "message": "ok", "data": {}})


@pytest.mark.anyio
async def test_allowlisted_get_reaches_the_inner_transport() -> None:
    """Either allowlisted path, requested with GET, reaches the real transport."""
    inner = _RecordingInnerTransport()
    transport = ReadOnlyTransport(inner)
    for path in ALLOWED_PATHS:
        request = httpx.Request("GET", f"https://api.ecoflow.com{path}")
        response = await transport.handle_async_request(request)
        assert response.status_code == 200
    assert inner.calls == len(ALLOWED_PATHS)


@pytest.mark.anyio
async def test_non_allowlisted_path_is_rejected_before_any_io() -> None:
    """A GET to any other path raises before the inner transport is ever reached."""
    inner = _RecordingInnerTransport()
    transport = ReadOnlyTransport(inner)
    request = httpx.Request("GET", "https://api.ecoflow.com/iot-open/sign/device/quota/set")
    with pytest.raises(NonAllowlistedRequest):
        await transport.handle_async_request(request)
    assert inner.calls == 0


@pytest.mark.anyio
async def test_non_get_method_on_an_allowlisted_path_is_rejected() -> None:
    """A POST to an otherwise-allowlisted path still raises before any I/O —
    there is no way to turn a read endpoint into a write one by method alone."""
    inner = _RecordingInnerTransport()
    transport = ReadOnlyTransport(inner)
    request = httpx.Request("POST", "https://api.ecoflow.com/iot-open/sign/device/list")
    with pytest.raises(NonAllowlistedRequest):
        await transport.handle_async_request(request)
    assert inner.calls == 0


def test_client_public_surface_is_exactly_list_devices_and_fetch_quota() -> None:
    """No third public method exists on the client — there is no code path
    to issue any request beyond the two allowlisted reads."""
    public_members = {
        name
        for name, _ in inspect.getmembers(EcoFlowCloudClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_members == {"list_devices", "fetch_quota"}


def test_no_module_outside_ecoflow_client_contains_the_iot_open_path() -> None:
    """The literal EcoFlow endpoint prefix lives in exactly one module."""
    offenders = [
        str(path.relative_to(_SRC_ROOT))
        for path in _SRC_ROOT.rglob("*.py")
        if path.name != "ecoflow_client.py" and "/iot-open/" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "module_name",
    ["paho.mqtt.client", "paho", "gmqtt", "aiomqtt", "amqtt", "hbmqtt"],
)
def test_no_mqtt_library_is_importable(module_name: str) -> None:
    """No MQTT client library exists in this dependency tree — v1 is
    read-only cloud polling only; MQTT acquisition was explicitly deferred."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
