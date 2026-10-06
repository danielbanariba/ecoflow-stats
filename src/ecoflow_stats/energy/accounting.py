"""Pure daily energy accounting: counter-delta segmentation, the
monotonicity guard, the implausible-jump guard, gap-aware proration, and
tariff-based cost (energy spec: "Daily Energy by Source and Direction",
"Counter Monotonicity Guard", "Energy Across a Data Gap Is Flagged",
"Energy Cost from a Configurable Flat Tariff", "Cost Basis Is Energy
Drawn From the Grid, Not Energy Delivered to Loads"; Named Defect
"Counter reset"; design-data section 4.5).

Pure domain logic, no I/O: `energy` is one of the packages
`tests/contract/test_pure_core_imports.py` scans for a forbidden
`storage`/`sqlite3`/... import. This module only ever shapes data its
caller already queried; the orchestration layer that queries `storage`
through `ports.py` and hands samples in here is `energy.service`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ecoflow_stats.timeutil import local_day, split_by_local_days

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading

#: The device's 5 cumulative Wh counters this module accounts for, in the
#: order `daily_rollups`' columns store them (design-data section 2's DDL).
COUNTER_FIELDS: tuple[str, ...] = (
    "chg_ac_wh",
    "chg_dc_wh",
    "chg_solar_wh",
    "dsg_ac_wh",
    "dsg_dc_wh",
)

#: Above this average power (W), a delta is implausible for its elapsed
#: interval and is discarded rather than counted (design-data section 4.5).
MAX_PLAUSIBLE_W = 10_000.0

#: A gap at least this long (s) between two consecutive counter readings
#: makes that delta's day(s) gap-affected and -- for `chg_ac_wh` only --
#: tracked separately as an estimate, matching config
#: `ECOFLOW_STATS_GAP_THRESHOLD`'s own default.
DEFAULT_GAP_THRESHOLD_S = 150

#: The single counter the energy cost basis is computed from (spec
#: requirement "Cost Basis Is Energy Drawn From the Grid, Not Energy
#: Delivered to Loads"): AC energy charged into the station from the
#: grid, passthrough included.
COST_BASIS_FIELD = "chg_ac_wh"


@dataclass(frozen=True, slots=True)
class DailyEnergy:
    """One local calendar day's accounted energy, by source and
    direction, in Wh, plus its tariff-priced cost. Every numeric field
    defaults to `0.0` -- a day only appears in `daily_energy`'s result at
    all once at least one counter actually contributed or flagged it, so
    a `0.0` here means "no energy moved", never "unknown"."""

    day: str
    chg_ac_wh: float = 0.0
    chg_dc_wh: float = 0.0
    chg_solar_wh: float = 0.0
    dsg_ac_wh: float = 0.0
    dsg_dc_wh: float = 0.0
    chg_ac_est_wh: float = 0.0
    flags: frozenset[str] = frozenset()
    cost: float | Literal["unavailable"] = "unavailable"
    currency: str = ""


@dataclass(frozen=True, slots=True)
class _SegmentOutcome:
    """What one consecutive pair of counter readings contributes:
    `delta_wh` is the energy to attribute (`0.0` when discarded), `flag`
    is the single reason it was discarded (`None` when counted cleanly),
    and `gap_prorated` says whether the elapsed interval exceeded the
    gap threshold. One shared decision point for all 3 guards a daily
    energy calculation needs (monotonicity, implausible jump, gap
    proration) -- task 18.5's refactor: not three independently written
    ad hoc checks that happen to look similar.
    """

    delta_wh: float
    flag: Literal["counter_reset", "implausible_jump"] | None
    gap_prorated: bool


def _segment_outcome(
    prev_value: float,
    value: float,
    elapsed_s: int,
    *,
    max_plausible_w: float,
    gap_threshold_s: int,
) -> _SegmentOutcome:
    """Classify one counter delta between two consecutive readings
    `elapsed_s` apart.

    A lower reading (Named Defect "Counter reset") contributes zero
    across the drop itself; the segment after it still resumes from its
    own new baseline, simply because the caller always diffs against
    the immediately preceding RAW reading regardless of this function's
    verdict, never against a pre-reset value. A delta implausible for
    its elapsed interval (energy requirement's sibling guard, scenario
    "A delta implausible for the elapsed interval ... is flagged") is
    likewise discarded. Anything else is counted, flagged `gap_prorated`
    only when the interval exceeded `gap_threshold_s` (energy
    requirement "Energy Across a Data Gap Is Flagged").
    """
    delta = value - prev_value
    if delta < 0:
        return _SegmentOutcome(delta_wh=0.0, flag="counter_reset", gap_prorated=False)
    if elapsed_s > 0 and delta > max_plausible_w * elapsed_s / 3600:
        return _SegmentOutcome(delta_wh=0.0, flag="implausible_jump", gap_prorated=False)
    return _SegmentOutcome(delta_wh=delta, flag=None, gap_prorated=elapsed_s > gap_threshold_s)


def _accumulate_counter(
    readings: Sequence[tuple[int, float | None]],
    field: str,
    *,
    tz: str,
    max_plausible_w: float,
    gap_threshold_s: int,
    totals: dict[str, dict[str, float]],
    flags: dict[str, set[str]],
) -> None:
    """Fold one counter's readings (in `ts` order) into `totals`/`flags`,
    in place. `daily_energy` calls this once per field in
    `COUNTER_FIELDS`, exactly as design-data section 4.5 describes ("for
    counter in (chg_ac_wh, ...)"), so every field's own counter segments
    are tracked independently of every other field's.
    """
    prev: tuple[int, float] | None = None
    for ts, value in readings:
        if value is None:
            continue
        if prev is not None:
            prev_ts, prev_value = prev
            outcome = _segment_outcome(
                prev_value,
                value,
                ts - prev_ts,
                max_plausible_w=max_plausible_w,
                gap_threshold_s=gap_threshold_s,
            )
            if outcome.flag is not None:
                day = local_day(ts, tz)
                totals.setdefault(day, {})
                flags.setdefault(day, set()).add(outcome.flag)
            else:
                for day, share in split_by_local_days(prev_ts, ts, tz):
                    day_totals = totals.setdefault(day, {})
                    day_totals[field] = day_totals.get(field, 0.0) + outcome.delta_wh * share
                    if outcome.gap_prorated:
                        flags.setdefault(day, set()).add("gap_prorated")
                        if field == COST_BASIS_FIELD:
                            day_totals["chg_ac_est_wh"] = (
                                day_totals.get("chg_ac_est_wh", 0.0) + outcome.delta_wh * share
                            )
        prev = (ts, value)


def daily_energy(
    samples: Sequence[tuple[int, Reading]],
    *,
    tz: str,
    tariff: float | None,
    currency: str,
    max_plausible_w: float = MAX_PLAUSIBLE_W,
    gap_threshold_s: int = DEFAULT_GAP_THRESHOLD_S,
) -> dict[str, DailyEnergy]:
    """Daily energy by source and direction, keyed by local calendar day
    (energy requirement "Daily Energy by Source and Direction"), from an
    arbitrarily long, `ts`-ordered batch of samples.

    Each of `COUNTER_FIELDS` is accounted independently (its own
    monotonicity/implausibility/gap guard, since the 5 counters can and
    do report at different cadences), then assembled into one
    `DailyEnergy` per day. Cost is computed at read time from
    `COST_BASIS_FIELD`'s total (energy requirement "Energy Cost from a
    Configurable Flat Tariff"): `None` for `tariff` reports cost as
    `"unavailable"`, never a fabricated `0.0` (scenario "No configured
    tariff shows energy without a fabricated cost").
    """
    totals: dict[str, dict[str, float]] = {}
    flags: dict[str, set[str]] = {}
    for field in COUNTER_FIELDS:
        readings = [(ts, getattr(reading, field)) for ts, reading in samples]
        _accumulate_counter(
            readings,
            field,
            tz=tz,
            max_plausible_w=max_plausible_w,
            gap_threshold_s=gap_threshold_s,
            totals=totals,
            flags=flags,
        )

    result: dict[str, DailyEnergy] = {}
    for day, day_totals in totals.items():
        chg_ac_wh = day_totals.get(COST_BASIS_FIELD, 0.0)
        cost: float | Literal["unavailable"] = (
            "unavailable" if tariff is None else chg_ac_wh / 1000 * tariff
        )
        result[day] = DailyEnergy(
            day=day,
            chg_ac_wh=chg_ac_wh,
            chg_dc_wh=day_totals.get("chg_dc_wh", 0.0),
            chg_solar_wh=day_totals.get("chg_solar_wh", 0.0),
            dsg_ac_wh=day_totals.get("dsg_ac_wh", 0.0),
            dsg_dc_wh=day_totals.get("dsg_dc_wh", 0.0),
            chg_ac_est_wh=day_totals.get("chg_ac_est_wh", 0.0),
            flags=frozenset(flags.get(day, set())),
            cost=cost,
            currency=currency,
        )
    return result


__all__ = [
    "COST_BASIS_FIELD",
    "COUNTER_FIELDS",
    "DEFAULT_GAP_THRESHOLD_S",
    "MAX_PLAUSIBLE_W",
    "DailyEnergy",
    "daily_energy",
]
