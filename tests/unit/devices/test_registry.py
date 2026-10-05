"""Unit tests for AdapterRegistry.resolve()."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ecoflow_stats.devices.adapter import FieldSpec, MappedAdapter
from ecoflow_stats.devices.models.generic import GenericAdapter
from ecoflow_stats.devices.registry import AdapterRegistry

_UNKNOWN_MODEL_PAYLOAD = json.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures" / "payloads" / "unknown_model.json"
    ).read_text()
)


class _FakeSpecificAdapter(MappedAdapter):
    """A minimal stand-in for a model-specific adapter: proves the registry
    prefers a claiming adapter over the generic fallback, and that adding
    one does not disturb another model's output."""

    adapter_id = "fake-specific"
    FIELDS: Mapping[str, FieldSpec] = {"soc": FieldSpec(keys=("soc",), cast=round)}

    def claims(self, info: object, payload: object) -> bool:
        return isinstance(payload, dict) and payload.get("model") == "fake-specific-model"


def test_unregistered_model_resolves_to_generic_adapter() -> None:
    """An unrecognized model identifier is still resolved -- never rejected
    -- by falling back to the generic adapter."""
    registry = AdapterRegistry([GenericAdapter()])
    adapter = registry.resolve(explicit_adapter_id=None, info=None, payload=_UNKNOWN_MODEL_PAYLOAD)
    assert adapter.adapter_id == "generic"


def test_generic_adapter_normalizes_an_unknown_payload_with_unmapped_fields_null() -> None:
    """The unknown-model fixture normalizes without error: its one mapped
    field (soc, via the case-insensitive ems.lcdShowSoc key) is populated,
    every other field stays NULL, and the key the adapter cannot map is
    reported, never silently dropped."""
    registry = AdapterRegistry([GenericAdapter()])
    adapter = registry.resolve(explicit_adapter_id=None, info=None, payload=_UNKNOWN_MODEL_PAYLOAD)
    normalized = adapter.normalize(_UNKNOWN_MODEL_PAYLOAD)
    assert normalized.reading.soc == 55
    assert normalized.reading.grid_v is None
    assert "some.unknown.key" in normalized.unmapped_keys


def test_registering_a_new_adapter_does_not_change_another_models_output() -> None:
    """Adding a claiming adapter for one model must not alter what the
    generic adapter produces for a payload it does not claim."""
    generic = GenericAdapter()
    baseline = generic.normalize({"soc": 55})

    registry = AdapterRegistry([_FakeSpecificAdapter(), generic])
    resolved_for_other_model = registry.resolve(
        explicit_adapter_id=None, info=None, payload={"soc": 55}
    )

    assert resolved_for_other_model.adapter_id == "generic"
    assert resolved_for_other_model.normalize({"soc": 55}) == baseline


def test_claiming_adapter_is_chosen_over_the_generic_fallback() -> None:
    registry = AdapterRegistry([_FakeSpecificAdapter(), GenericAdapter()])
    adapter = registry.resolve(
        explicit_adapter_id=None, info=None, payload={"model": "fake-specific-model", "soc": 77}
    )
    assert adapter.adapter_id == "fake-specific"


def test_explicit_adapter_id_overrides_claims_based_resolution() -> None:
    registry = AdapterRegistry([_FakeSpecificAdapter(), GenericAdapter()])
    adapter = registry.resolve(
        explicit_adapter_id="generic", info=None, payload={"model": "fake-specific-model"}
    )
    assert adapter.adapter_id == "generic"
