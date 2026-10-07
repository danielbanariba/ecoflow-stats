"""Unit tests for the DELTA Pro adapter's verified key mapping.

Key names and bounds come from the verified schema-v1 mapping
(`ecoflow-panel/core/ecoflow-power-watch`, `docs/API.md`): all fields are
confirmed non-NULL against the live device except the two BMS capacity
counters and the OCV reading, which carry hardware sentinel values when
unavailable and are never mapped to any Reading field at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo
from ecoflow_stats.devices.models.delta_pro import DeltaProAdapter
from ecoflow_stats.devices.models.generic import GenericAdapter
from ecoflow_stats.devices.registry import AdapterRegistry

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "payloads"
_FULL_PAYLOAD = json.loads((_FIXTURES / "delta_pro_full.json").read_text())
_PARTIAL_PAYLOAD = json.loads((_FIXTURES / "delta_pro_partial.json").read_text())


def test_full_payload_maps_every_verified_field() -> None:
    reading = DeltaProAdapter().normalize(_FULL_PAYLOAD).reading
    assert reading.grid_v == 121.7
    assert reading.grid_hz == 60.0
    assert reading.ac_in_w == 850.0
    assert reading.ac_out_w == 0.0
    assert reading.in_w == 850.0
    assert reading.out_w == 620.0
    assert reading.solar_in_w == 0.0
    assert reading.batt_in_w == 820.0
    assert reading.batt_out_w == 0.0
    assert reading.batt_temp_c == 28.5
    assert reading.chg_ac_wh == 2026.4
    assert reading.chg_dc_wh == 0.0
    assert reading.chg_solar_wh == 0.0
    assert reading.dsg_ac_wh == 1904.1
    assert reading.dsg_dc_wh == 0.0
    assert reading.cycles == 142
    assert reading.soh == 98.0
    assert reading.chg_remain_min == 326
    assert reading.dsg_remain_min == 0
    assert reading.soc == 97


def test_bms_sentinel_keys_are_never_mapped_to_any_field() -> None:
    """totalChgCap/totalDsgCap/ecloudOcv carry hardware sentinels and have
    no corresponding Reading field at all -- they must stay unconsumed, so
    a future change cannot accidentally wire one to a field without
    confronting its sentinel contamination."""
    normalized = DeltaProAdapter().normalize(_FULL_PAYLOAD)
    assert "bmsMaster.totalChgCap" in normalized.unmapped_keys
    assert "bmsMaster.totalDsgCap" in normalized.unmapped_keys
    assert "bmsMaster.ecloudOcv" in normalized.unmapped_keys


def test_claims_a_payload_carrying_the_delta_pro_key_family() -> None:
    assert DeltaProAdapter().claims(None, _FULL_PAYLOAD) is True


def test_claims_by_product_name_even_without_the_key_family() -> None:
    info = DeviceInfo(sn="TESTDP0001", name="Garage", product_name="EcoFlow DELTA Pro", online=True)
    assert DeltaProAdapter().claims(info, {}) is True


def test_does_not_claim_an_unrelated_payload() -> None:
    assert DeltaProAdapter().claims(None, {"some.other.key": 1}) is False


def test_registered_model_uses_the_specific_adapter_over_generic() -> None:
    """Scenario: 'A registered model uses its specific adapter.'"""
    registry = AdapterRegistry([DeltaProAdapter(), GenericAdapter()])
    adapter = registry.resolve(explicit_adapter_id=None, info=None, payload=_FULL_PAYLOAD)
    assert adapter.adapter_id == "delta_pro"


def test_partial_payload_normalizes_without_error_with_unmapped_fields_none() -> None:
    normalized = DeltaProAdapter().normalize(_PARTIAL_PAYLOAD)
    assert normalized.reading.grid_v == 121.7
    assert normalized.reading.chg_ac_wh == 2026.4
    assert normalized.reading.cycles is None
    assert normalized.reading.soh is None
    assert normalized.reading.soc is None
