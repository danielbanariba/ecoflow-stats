"""Pure grid quality: ordered voltage/frequency history, and min/avg/max
aggregation over grid-PRESENT judged readings for an arbitrary batch of
samples -- one calendar day (`rollups.service.derive_rollups`'s per-day
call) or one display bucket (`web.routes.api`'s `grid/series` route);
the caller decides what the batch means (grid-quality spec: "Grid
Voltage and Frequency History", "Daily Voltage and Frequency Ranges",
"Grid-Quality Metrics Exclude Periods Without Grid Voltage").

Reuses `outages.model.judge()` for grid presence -- the exact same
threshold and stale-payload rule the detector and live status already
use (design D6) -- rather than a second hand-rolled `grid_v > threshold`
comparison.

Pure domain logic, no I/O: `grid` is one of the packages
`tests/contract/test_pure_core_imports.py` scans for a forbidden
`storage`/`sqlite3`/... import (Named Defect "Core doing I/O").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ecoflow_stats.outages.model import DetectorConfig, judge

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading

_DEFAULT_CONFIG = DetectorConfig()


@dataclass(frozen=True, slots=True)
class GridPoint:
    """One sample's grid voltage and frequency, timestamped."""

    ts: int
    grid_v: float | None
    grid_hz: float | None


@dataclass(frozen=True, slots=True)
class GridRange:
    """One batch's min/avg/max voltage and frequency, over grid-PRESENT
    judged readings only. `grid_v_*` can never be `None` here -- `judge()`
    itself never judges a NULL-voltage sample `present` -- but
    `grid_hz_*` is `None` when every present reading in the batch also
    happened to lack frequency, the same NULL-discipline
    `rollups.service._daily_battery_fields` already applies to
    `soc_min`/`max`/`batt_temp_max`."""

    grid_v_min: float
    grid_v_avg: float
    grid_v_max: float
    grid_hz_min: float | None
    grid_hz_avg: float | None
    grid_hz_max: float | None
    readings: int


def voltage_frequency_history(samples: Sequence[tuple[int, Reading]]) -> list[GridPoint]:
    """The voltage/frequency series for a requested period, in timestamp
    order (grid-quality requirement "Grid Voltage and Frequency
    History", scenario "History reflects recorded samples in order").

    Sorts defensively by ``ts``, the same convention
    `battery.stats.charge_history` already established for this
    project's other per-sample series.
    """
    return [
        GridPoint(ts=ts, grid_v=reading.grid_v, grid_hz=reading.grid_hz)
        for ts, reading in sorted(samples, key=lambda pair: pair[0])
    ]


def grid_quality_range(
    samples: Sequence[tuple[int, Reading]], *, config: DetectorConfig = _DEFAULT_CONFIG
) -> GridRange | Literal["unavailable"]:
    """Min/avg/max voltage and frequency over this batch's grid-PRESENT
    judged readings only (grid-quality requirement "Daily Voltage and
    Frequency Ranges", scenario "A normal day reports a voltage and
    frequency range"). Every judged-absent or judged-unjudged reading
    (including a NULL-voltage one, which `judge()` always reports
    unjudged) is excluded, never pulling the minimum toward zero
    (requirement "Grid-Quality Metrics Exclude Periods Without Grid
    Voltage", scenario "A NULL voltage sample does not pull the range
    toward zero"). An empty batch, or one with no PRESENT-judged
    reading at all, reports `"unavailable"`, never a fabricated `0.0`
    (scenario "A day with no grid-present samples reports no range").

    The caller decides what one batch means -- a calendar day or a
    display bucket -- `judge()`'s own sliding window is built fresh
    from just this batch's own samples in `ts` order, so the very first
    reading in a batch can never trigger the stale-payload guard (the
    same per-batch-independent tradeoff
    `rollups.service._daily_battery_fields` already makes for the
    battery fields, rather than carrying cross-batch judge context).
    """
    ordered = sorted(samples, key=lambda pair: pair[0])
    window: list[Reading] = []
    voltages: list[float] = []
    frequencies: list[float] = []
    present_count = 0
    for _ts, reading in ordered:
        judgment = judge(reading, window, config=config)
        window.append(reading)
        if len(window) > config.stale_repeat - 1:
            window.pop(0)
        if judgment.state != "present":
            continue
        present_count += 1
        # `judge()` never judges a NULL-voltage reading `present` (its own
        # first line returns UNJUDGED for exactly that case), so `grid_v`
        # is guaranteed non-None here -- no separate NULL guard needed for
        # voltage. `grid_hz` carries no such guarantee: `judge()` never
        # looks at it, so a present reading can still lack frequency.
        voltages.append(reading.grid_v)  # type: ignore[arg-type]
        if reading.grid_hz is not None:
            frequencies.append(reading.grid_hz)
    if present_count == 0:
        return "unavailable"
    return GridRange(
        grid_v_min=min(voltages),
        grid_v_avg=sum(voltages) / len(voltages),
        grid_v_max=max(voltages),
        grid_hz_min=min(frequencies) if frequencies else None,
        grid_hz_avg=(sum(frequencies) / len(frequencies)) if frequencies else None,
        grid_hz_max=max(frequencies) if frequencies else None,
        readings=present_count,
    )


__all__ = ["GridPoint", "GridRange", "grid_quality_range", "voltage_frequency_history"]
