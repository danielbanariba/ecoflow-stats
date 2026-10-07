"""The DELTA Pro adapter: Daniel's own unit, verified live against the
real device (``ecoflow-panel/core/ecoflow-power-watch``,
``docs/API.md``).

``bmsMaster.totalChgCap``/``totalDsgCap`` and ``bmsMaster.ecloudOcv`` carry
hardware sentinel values (``0xFFFFFFFF``/``65535``) when the BMS has no
real figure to report, and — unlike every other field here — have no
corresponding ``Reading`` field at all: they are deliberately never
mapped, so this adapter cannot accidentally wire one to a field without
confronting its sentinel contamination first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.devices.adapter import FieldSpec, MappedAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ecoflow_stats.devices.adapter import DeviceInfo

_WATT_BOUNDS = (0.0, 20_000.0)
_COUNTER_BOUNDS = (0.0, 1e12)
_KEY_FAMILIES = ("bmsMaster.", "ems.", "inv.", "pd.")


def _round_int(value: float) -> int:
    return round(value)


class DeltaProAdapter(MappedAdapter):
    """Model-specific adapter for the EcoFlow DELTA Pro."""

    adapter_id = "delta_pro"

    FIELDS: Mapping[str, FieldSpec] = {
        "soc": FieldSpec(
            keys=("ems.lcdShowSoc", "bmsMaster.soc", "bms_bmsStatus.soc", "pd.soc", "soc"),
            cast=_round_int,
            bounds=(0.0, 100.0),
        ),
        "grid_v": FieldSpec(
            keys=("inv.acInVol",), transform=lambda v: v / 1000.0, bounds=(0.0, 300.0)
        ),
        "grid_hz": FieldSpec(keys=("inv.acInFreq",), bounds=(0.0, 70.0)),
        "ac_in_w": FieldSpec(keys=("inv.inputWatts",), bounds=_WATT_BOUNDS),
        "ac_out_w": FieldSpec(keys=("inv.outputWatts",), bounds=_WATT_BOUNDS),
        "in_w": FieldSpec(keys=("pd.wattsInSum",), bounds=_WATT_BOUNDS),
        "out_w": FieldSpec(keys=("pd.wattsOutSum",), bounds=_WATT_BOUNDS),
        "solar_in_w": FieldSpec(keys=("mppt.inWatts",), bounds=_WATT_BOUNDS),
        "batt_in_w": FieldSpec(keys=("bmsMaster.inputWatts",), bounds=_WATT_BOUNDS),
        "batt_out_w": FieldSpec(keys=("bmsMaster.outputWatts",), bounds=_WATT_BOUNDS),
        "batt_temp_c": FieldSpec(keys=("bmsMaster.temp",)),
        "chg_ac_wh": FieldSpec(keys=("pd.chgPowerAc",), bounds=_COUNTER_BOUNDS),
        "chg_dc_wh": FieldSpec(keys=("pd.chgPowerDc",), bounds=_COUNTER_BOUNDS),
        "chg_solar_wh": FieldSpec(keys=("pd.chgSunPower",), bounds=_COUNTER_BOUNDS),
        "dsg_ac_wh": FieldSpec(keys=("pd.dsgPowerAc",), bounds=_COUNTER_BOUNDS),
        "dsg_dc_wh": FieldSpec(keys=("pd.dsgPowerDc",), bounds=_COUNTER_BOUNDS),
        "cycles": FieldSpec(keys=("bmsMaster.cycles",), cast=_round_int, bounds=_COUNTER_BOUNDS),
        "soh": FieldSpec(keys=("bmsMaster.soh",), bounds=(0.0, 100.0)),
        "chg_remain_min": FieldSpec(
            keys=("ems.chgRemainTime",), cast=_round_int, bounds=_COUNTER_BOUNDS
        ),
        "dsg_remain_min": FieldSpec(
            keys=("ems.dsgRemainTime",), cast=_round_int, bounds=_COUNTER_BOUNDS
        ),
    }

    def claims(self, info: DeviceInfo | None, payload: Mapping[str, object]) -> bool:
        if info is not None and info.product_name and "DELTA Pro" in info.product_name:
            return True
        has_key_family = any(key.startswith(_KEY_FAMILIES) for key in payload)
        return has_key_family and "pd.chgSunPower" in payload


__all__ = ["DeltaProAdapter"]
