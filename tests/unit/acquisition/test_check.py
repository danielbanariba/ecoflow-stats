"""Unit tests for the one-shot `check` command.

Covers acquisition's "One-Shot Check Command" requirement: exactly one
`list_devices()` and one `fetch_quota()` call, no database writes (there is
no storage dependency here at all to begin with), a normalized reading
printed with NULL fields marked, unmapped key *names* surfaced without
their values, and no configured secret ever appearing in the output.
"""

from __future__ import annotations

import io

import pytest

from ecoflow_stats.acquisition.check import mask_serial, run_check
from ecoflow_stats.acquisition.ecoflow_client import CloudApiError, DeviceInfo
from ecoflow_stats.devices.models.generic import GenericAdapter
from ecoflow_stats.devices.registry import AdapterRegistry
from tests.fakes import FakeDeviceCloud

_SERIAL = "TESTSERIAL0001"
_SENSITIVE_PAYLOAD_VALUE = "TOP-SECRET-SHOULD-NEVER-APPEAR"


def _registry() -> AdapterRegistry:
    return AdapterRegistry([GenericAdapter()])


@pytest.mark.anyio
async def test_check_prints_a_normalized_reading_with_null_fields_marked() -> None:
    cloud = FakeDeviceCloud(
        devices=[DeviceInfo(sn=_SERIAL, name="Garage", product_name="Mystery", online=True)],
        quota_results={_SERIAL: {"ems.lcdShowSoc": 55, "inv.acInVol": 121700}},
    )
    out = io.StringIO()
    exit_code = await run_check(
        configured_serials=[_SERIAL],
        requested_serial=None,
        cloud=cloud,
        registry=_registry(),
        out=out,
    )
    text = out.getvalue()
    assert exit_code == 0
    assert cloud.list_devices_calls == 1
    assert cloud.fetch_quota_calls == [_SERIAL]
    assert "soc = 55" in text
    assert "grid_v = 121.7" in text
    assert "grid_hz = None (NULL)" in text


@pytest.mark.anyio
async def test_check_surfaces_unmapped_key_names_without_their_values() -> None:
    cloud = FakeDeviceCloud(
        devices=[DeviceInfo(sn=_SERIAL, name="Garage", product_name="Mystery", online=True)],
        quota_results={_SERIAL: {"some.unmapped.key": _SENSITIVE_PAYLOAD_VALUE}},
    )
    out = io.StringIO()
    await run_check(
        configured_serials=[_SERIAL],
        requested_serial=None,
        cloud=cloud,
        registry=_registry(),
        out=out,
    )
    text = out.getvalue()
    assert "some.unmapped.key" in text
    assert _SENSITIVE_PAYLOAD_VALUE not in text


@pytest.mark.anyio
async def test_check_masks_the_serial_to_its_last_four_characters() -> None:
    cloud = FakeDeviceCloud(
        devices=[DeviceInfo(sn=_SERIAL, name="Garage", product_name="Mystery", online=True)],
        quota_results={_SERIAL: {}},
    )
    out = io.StringIO()
    await run_check(
        configured_serials=[_SERIAL],
        requested_serial=None,
        cloud=cloud,
        registry=_registry(),
        out=out,
    )
    text = out.getvalue()
    assert mask_serial(_SERIAL) in text
    assert _SERIAL not in text


@pytest.mark.anyio
async def test_check_exits_2_when_the_requested_serial_is_not_configured() -> None:
    cloud = FakeDeviceCloud(devices=[], quota_results={})
    out = io.StringIO()
    exit_code = await run_check(
        configured_serials=[_SERIAL],
        requested_serial="SOMETHING-ELSE",
        cloud=cloud,
        registry=_registry(),
        out=out,
    )
    assert exit_code == 2
    assert cloud.list_devices_calls == 0


@pytest.mark.anyio
async def test_check_exits_2_when_no_serial_is_configured_or_given() -> None:
    cloud = FakeDeviceCloud(devices=[], quota_results={})
    out = io.StringIO()
    exit_code = await run_check(
        configured_serials=[], requested_serial=None, cloud=cloud, registry=_registry(), out=out
    )
    assert exit_code == 2


@pytest.mark.anyio
async def test_check_exits_3_on_an_api_error() -> None:
    cloud = FakeDeviceCloud(
        devices=[],
        quota_results={_SERIAL: CloudApiError("8521", "signature is wrong")},
    )
    out = io.StringIO()
    exit_code = await run_check(
        configured_serials=[_SERIAL],
        requested_serial=None,
        cloud=cloud,
        registry=_registry(),
        out=out,
    )
    assert exit_code == 3
