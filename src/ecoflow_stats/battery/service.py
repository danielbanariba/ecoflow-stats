"""Observed autonomy: comparing how long the battery actually carried
the load during a real outage against the device's own remaining-time
estimate at the moment that outage began (battery requirement
"Observed Autonomy Compared With Device Estimate"; design-data section
4.6; amendment item 7).

Pure domain logic, no I/O: `battery` is one of the packages
`tests/contract/test_pure_core_imports.py` scans for a forbidden
`storage`/`sqlite3`/... import (Named Defect "Core doing I/O"). This
module composes `battery.stats.depth_of_discharge` for its own SoC-
drop qualification check rather than re-deriving the `soc_start`/
`soc_min` NULL guard a second time -- `depth_of_discharge` already
reports `"unavailable"` on a missing boundary charge, which this
module folds into its own "not enough data" branch (task 17.11
REFACTOR: `battery.service` and `battery.stats` must not each
implement their own copy of that NULL-safety guard).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ecoflow_stats.battery.stats import depth_of_discharge

if TYPE_CHECKING:
    from ecoflow_stats.outages.model import Event

MIN_QUALIFYING_MINUTES = 15
"""An outage shorter than this has no reliable discharge rate to
extrapolate an autonomy figure from (design-data section 4.6:
"outages of at least 15 min")."""
MIN_QUALIFYING_DROP = 3
"""A SoC drop smaller than this is too close to sensor/reporting noise
to extrapolate a reliable hourly rate from (design-data section 4.6:
"a SoC drop of at least 3 points")."""


@dataclass(frozen=True, slots=True)
class ObservedAutonomy:
    """One qualifying outage's observed battery autonomy, compared
    with the device's own remaining-time estimate at the moment it
    began."""

    event_start_ts: int
    observed_h: float
    """Hours the battery would have lasted at the discharge rate
    actually observed during this outage, extrapolated from
    `soc_start` -- always a number once an outage qualifies at all."""
    device_estimate_h: float | Literal["unavailable"]
    """The device's own `dsg_remain_min_start` estimate, in hours --
    `"unavailable"` (never fabricated as `0` or silently dropped) when
    the device reported no estimate at that outage's start (amendment
    item 7)."""


def observed_autonomy(event: Event) -> ObservedAutonomy | Literal["not enough data"]:
    """Compare one outage's observed battery autonomy with the
    device's own estimate (design-data section 4.6).

    Reports `"not enough data"` when the outage is still ongoing (no
    final duration or final `soc_min` to extrapolate from yet), too
    short (< 15 min), too shallow (< 3 SoC points), or when
    `depth_of_discharge` itself cannot compute a drop at all (a
    missing boundary charge) -- the same qualification failure as
    "too short/too shallow", reported identically rather than as a
    separate, confusing case.

    For a qualifying outage, `observed_h` is always a number; only the
    *comparison* against the device's own estimate may be
    `"unavailable"`, when `dsg_remain_min_start` is `None` (amendment
    item 7) -- distinct from, and never conflated with, "not enough
    data".
    """
    if event.end_ts is None:
        return "not enough data"

    duration_min = (event.end_ts - event.start_ts) / 60
    if duration_min < MIN_QUALIFYING_MINUTES:
        return "not enough data"

    drop = depth_of_discharge(event)
    if drop == "unavailable" or drop < MIN_QUALIFYING_DROP:
        return "not enough data"

    # `depth_of_discharge` only returns a number when both `soc_start`
    # and `soc_min` are known -- `soc_start` is therefore guaranteed
    # not `None` here, the same guarantee `drop`'s own type narrows.
    assert event.soc_start is not None
    duration_h = duration_min / 60
    rate = drop / duration_h
    observed_h = event.soc_start / rate

    device_estimate_h: float | Literal["unavailable"] = (
        event.dsg_remain_min_start / 60 if event.dsg_remain_min_start is not None else "unavailable"
    )
    return ObservedAutonomy(
        event_start_ts=event.start_ts, observed_h=observed_h, device_estimate_h=device_estimate_h
    )


__all__ = ["MIN_QUALIFYING_DROP", "MIN_QUALIFYING_MINUTES", "ObservedAutonomy", "observed_autonomy"]
