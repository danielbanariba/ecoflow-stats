"""The normalized device reading every adapter produces.

Every field is optional. A missing or unmappable source value is ``None``
here, never ``0`` — a device that is not reporting a figure is not the same
as a device reporting zero (Named Defect: missing read as zero).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Reading:
    """One device's normalized measurement, for one point in time."""

    soc: int | None = None
    grid_v: float | None = None
    grid_hz: float | None = None
    ac_in_w: float | None = None
    ac_out_w: float | None = None
    in_w: float | None = None
    out_w: float | None = None
    solar_in_w: float | None = None
    batt_in_w: float | None = None
    batt_out_w: float | None = None
    batt_temp_c: float | None = None
    chg_ac_wh: float | None = None
    chg_dc_wh: float | None = None
    chg_solar_wh: float | None = None
    dsg_ac_wh: float | None = None
    dsg_dc_wh: float | None = None
    cycles: int | None = None
    soh: float | None = None
    chg_remain_min: int | None = None
    dsg_remain_min: int | None = None


FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Reading))
"""Every ``Reading`` field name, in declaration order — the single source
of truth other modules (storage's column order, the outage judge's
whole-row staleness comparison) derive their own field lists from."""


__all__ = ["FIELD_NAMES", "Reading"]
