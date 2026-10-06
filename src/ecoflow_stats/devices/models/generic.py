"""The fallback adapter for any model without a registered, specific one.

Maps the same 20 fields a model-specific adapter would, using the DELTA
Pro's verified key names (``ecoflow-panel/docs/API.md``) as the primary
candidate for each field, matched case-insensitively — a model reporting
the same key family in different casing still yields useful data. A field
whose key is absent, or whose value does not parse, simply stays ``None``;
an unrecognized model is never rejected.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ecoflow_stats.devices.adapter import FieldSpec, MappedAdapter, _is_real_number

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ecoflow_stats.devices.adapter import DeviceInfo

_WATT_BOUNDS = (0.0, 20_000.0)
_COUNTER_BOUNDS = (0.0, math.inf)


def _round_int(value: float) -> int:
    return round(value)


class GenericAdapter(MappedAdapter):
    """Last-resort adapter: always claims, so it is the registry's catch-all."""

    adapter_id = "generic"

    FIELDS: Mapping[str, FieldSpec] = {
        "soc": FieldSpec(
            keys=("ems.lcdshowsoc", "bmsmaster.soc", "bms_bmsstatus.soc", "pd.soc", "soc"),
            cast=_round_int,
            bounds=(0.0, 100.0),
        ),
        "grid_v": FieldSpec(
            keys=("inv.acinvol",), transform=lambda v: v / 1000.0, bounds=(0.0, 300.0)
        ),
        "grid_hz": FieldSpec(keys=("inv.acinfreq",), bounds=(0.0, 70.0)),
        "ac_in_w": FieldSpec(keys=("inv.inputwatts",), bounds=_WATT_BOUNDS),
        "ac_out_w": FieldSpec(keys=("inv.outputwatts",), bounds=_WATT_BOUNDS),
        "in_w": FieldSpec(keys=("pd.wattsinsum",), bounds=_WATT_BOUNDS),
        "out_w": FieldSpec(keys=("pd.wattsoutsum",), bounds=_WATT_BOUNDS),
        "solar_in_w": FieldSpec(keys=("mppt.inwatts",), bounds=_WATT_BOUNDS),
        "batt_in_w": FieldSpec(keys=("bmsmaster.inputwatts",), bounds=_WATT_BOUNDS),
        "batt_out_w": FieldSpec(keys=("bmsmaster.outputwatts",), bounds=_WATT_BOUNDS),
        "batt_temp_c": FieldSpec(keys=("bmsmaster.temp",)),
        "chg_ac_wh": FieldSpec(keys=("pd.chgpowerac",), bounds=_COUNTER_BOUNDS),
        "chg_dc_wh": FieldSpec(keys=("pd.chgpowerdc",), bounds=_COUNTER_BOUNDS),
        "chg_solar_wh": FieldSpec(keys=("pd.chgsunpower",), bounds=_COUNTER_BOUNDS),
        "dsg_ac_wh": FieldSpec(keys=("pd.dsgpowerac",), bounds=_COUNTER_BOUNDS),
        "dsg_dc_wh": FieldSpec(keys=("pd.dsgpowerdc",), bounds=_COUNTER_BOUNDS),
        "cycles": FieldSpec(keys=("bmsmaster.cycles",), cast=_round_int, bounds=_COUNTER_BOUNDS),
        "soh": FieldSpec(keys=("bmsmaster.soh",), bounds=(0.0, 100.0)),
        "chg_remain_min": FieldSpec(
            keys=("ems.chgremaintime",), cast=_round_int, bounds=_COUNTER_BOUNDS
        ),
        "dsg_remain_min": FieldSpec(
            keys=("ems.dsgremaintime",), cast=_round_int, bounds=_COUNTER_BOUNDS
        ),
    }

    def claims(self, info: DeviceInfo | None, payload: Mapping[str, object]) -> bool:
        """Always claims: the registry only reaches this adapter after
        every model-specific adapter ahead of it has already declined."""
        return True

    def _resolve(
        self, payload: Mapping[str, object], keys: tuple[str, ...]
    ) -> tuple[float | None, str | None]:
        """Case-insensitive key matching, reporting the *original* payload
        key (not the lowercased candidate) so `unmapped_keys` reflects
        what the caller actually sent."""
        lowered_to_original = {str(key).lower(): key for key in payload}
        for key in keys:
            original = lowered_to_original.get(key)
            if original is None:
                continue
            value = payload.get(original)
            if _is_real_number(value):
                return float(value), original
        return None, None


__all__ = ["GenericAdapter"]
