"""Unit tests for the adapter mapping machinery: `num()`, `FieldSpec`, and
`MappedAdapter.normalize()`.

Exercised through a minimal local adapter so these tests prove the shared
machinery itself, independent of any specific device model's real field
table (covered separately in `test_registry.py` and, once it lands, the
DELTA Pro adapter's own tests).
"""

from __future__ import annotations

from collections.abc import Mapping

from ecoflow_stats.devices.adapter import FieldSpec, MappedAdapter


class _ProbeAdapter(MappedAdapter):
    adapter_id = "probe"
    FIELDS: Mapping[str, FieldSpec] = {
        "grid_v": FieldSpec(
            keys=("inv.acInVol",), transform=lambda v: v / 1000.0, bounds=(0.0, 300.0)
        ),
        "cycles": FieldSpec(keys=("bmsMaster.cycles",), cast=round),
        "chg_ac_wh": FieldSpec(keys=("bmsMaster.totalChgCap",), sentinels=(4_294_967_295.0,)),
    }

    def claims(self, info: object, payload: object) -> bool:
        return False


def test_absent_field_normalizes_to_none() -> None:
    """A field whose source key is missing from the payload must be None,
    never a fabricated zero (Named Defect: missing read as zero)."""
    normalized = _ProbeAdapter().normalize({})
    assert normalized.reading.grid_v is None
    assert "grid_v" not in normalized.rejected


def test_malformed_value_normalizes_to_none_not_a_parsed_zero() -> None:
    """A present-but-non-numeric value must be None, not coerced via a
    naive float() call that might raise or silently produce 0."""
    normalized = _ProbeAdapter().normalize({"inv.acInVol": "not-a-number"})
    assert normalized.reading.grid_v is None


def test_num_never_coerces_a_bool() -> None:
    """A JSON boolean must never be read as 1/0 -- bool is an int subclass
    in Python, so an unguarded isinstance(v, (int, float)) check would
    wrongly accept it."""
    normalized = _ProbeAdapter().normalize({"inv.acInVol": True})
    assert normalized.reading.grid_v is None


def test_present_numeric_value_is_transformed_and_cast() -> None:
    """A real numeric reading is divided by its transform and cast to the
    field's declared type -- this is the happy path the NULL-handling
    tests above must not accidentally make trivially true."""
    normalized = _ProbeAdapter().normalize({"inv.acInVol": 121700, "bmsMaster.cycles": 42.0})
    assert normalized.reading.grid_v == 121.7
    assert normalized.reading.cycles == 42
    assert normalized.rejected == {}


def test_sentinel_value_normalizes_to_none_and_is_rejected() -> None:
    """A hardware sentinel value (e.g. the BMS's 0xFFFFFFFF 'no data'
    marker) must be rejected, not reported as a legitimate huge reading."""
    normalized = _ProbeAdapter().normalize({"bmsMaster.totalChgCap": 4_294_967_295})
    assert normalized.reading.chg_ac_wh is None
    assert normalized.rejected["chg_ac_wh"] == "sentinel"


def test_out_of_bounds_value_normalizes_to_none_and_is_rejected() -> None:
    """A transformed value outside the field's physically plausible range
    is rejected rather than stored as a nonsensical reading."""
    normalized = _ProbeAdapter().normalize({"inv.acInVol": 999_000})  # 999 V, outside 0..300
    assert normalized.reading.grid_v is None
    assert normalized.rejected["grid_v"] == "out_of_bounds"


def test_unmapped_keys_lists_keys_no_field_consumed() -> None:
    """A payload key that no FieldSpec's candidate list names at all is
    reported as unmapped; a key that was matched is not."""
    normalized = _ProbeAdapter().normalize({"some.other.key": 1, "inv.acInVol": 121700})
    assert normalized.unmapped_keys == frozenset({"some.other.key"})
