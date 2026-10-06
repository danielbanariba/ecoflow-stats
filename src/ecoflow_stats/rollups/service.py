"""Daily rollup recompute orchestration (design-data section 4.8).

Decides whether a device's `daily_rollups` can resume from the dirty
day onward or need a full recompute, then re-aggregates each affected
day's stored samples into the battery-field columns (`cycles_last`,
`soh_last`, `soc_min`/`max`, `batt_temp_max`), the energy-field columns
(`chg_ac_wh`/`chg_dc_wh`/`chg_solar_wh`/`dsg_ac_wh`/`dsg_dc_wh`/
`chg_ac_est_wh`/`energy_flags`, visual-QA batch fix01, fix 6; composing
`energy.service.daily_energy_for_samples`), and, since task 20.2, the
grid-field columns (`grid_v_min`/`avg`/`max`, `grid_hz_min`/`avg`/`max`,
`grid_readings`; composing `grid.quality.grid_quality_range` directly
-- unlike energy, there is no `grid.service` composition seam, matching
this module's own existing precedent for the battery fields, which are
likewise computed inline below rather than through a `battery.service`
wrapper) -- one job run now produces every capability's columns for
the same row, matching `storage.rollups`'s "additively extensible"
design intent.

Grid presence (task 20.2) reuses `outages.model.judge()` through
`grid.quality.grid_quality_range`'s own default `DetectorConfig()`
rather than the application's actually-configured threshold/gap
settings: threading `Settings.outage_threshold_v`/`gap_threshold` into
this job would need `jobs.py`/`web.app._start_rollups_job` changes
outside this batch's declared edit surface, the same documented,
disclosed non-threading precedent batch fix01 already established for
`daily_energy_for_samples`'s `tariff=None`.

Mirrors `outages.service.derive_outages`'s shape closely: the caller
owns the surrounding `BEGIN IMMEDIATE` transaction and commits it once,
`rollup_store`/`derivation_store` never commit on their own (design D4).

Depends on `ecoflow_stats.ports` for `SampleStore`/`DerivationStore` --
the project's established pure-core-service style (D3) -- but on the
concrete `storage.rollups.RollupStore` directly rather than a `ports.py`
Protocol: the design's Ports section calls for a `RollupStore` Protocol
alongside `DeviceStore`/`LegacyStore`, but `ports.py` was outside this
slice's declared edit surface. A future slice whose edit surface
includes `ports.py` should add that Protocol and switch this import to
it, exactly as `rollups` itself is not (yet) one of
`tests/contract/test_pure_core_imports.py`'s scanned pure-core packages.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from ecoflow_stats.energy.service import daily_energy_for_samples
from ecoflow_stats.grid.quality import grid_quality_range
from ecoflow_stats.timeutil import day_bounds, local_day

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.ports import DerivationStore, SampleStore
    from ecoflow_stats.storage.rollups import RollupStore

_DERIVATION_NAME = "rollups"
_ROLLUP_VERSION = 1

_ENERGY_FLAG_BITS: dict[str, int] = {"counter_reset": 1, "implausible_jump": 2, "gap_prorated": 4}
"""Mirrors the `energy_flags` bitmask `storage.rollups`' DDL documents
(migration 0001: "1 counter_reset, 2 implausible_jump, 4 gap_prorated")
-- the same flag names `energy.accounting.DailyEnergy.flags` already
reports, encoded here for storage in a single INTEGER column (this
table's own DDL choice, unlike the sorted-string-join
`storage.legacy.LegacyStore` uses for its own `flags` column)."""


def _encode_energy_flags(flags: frozenset[str]) -> int:
    return sum(_ENERGY_FLAG_BITS[flag] for flag in flags)


def _params_hash() -> str:
    """A fingerprint of the configurable parameters that change a daily
    rollup's output, mirroring `outages.service._params_hash`'s role for
    the outage detector (design-data section 1: a parameter change
    forces a full recompute exactly like a version bump).

    No rollup parameter exists yet in this slice -- Phase 18 adds
    `ECOFLOW_STATS_TZ` (day boundaries) and may extend this hash with
    its actual value instead of the fixed placeholder below.
    """
    return hashlib.sha256(b"battery-only").hexdigest()


def _day_start(day: str, tz: str) -> int:
    """The UTC epoch second at which local calendar day ``day``
    (a ``'YYYY-MM-DD'`` string produced by a ``day_fn`` such as
    `timeutil.local_day`) begins under ``tz`` -- used only to widen an
    incremental recompute's query to the whole of its dirty day, never
    to enumerate days that have no samples. Delegates to
    `timeutil.day_bounds`, which this slice (Phase 18) closed the seam
    for: it is DST-aware (a day is 23 or 25 hours, not always exactly
    86_400 s), where this module's previous ``_utc_day_start``
    placeholder assumed every day was a fixed-length UTC day."""
    return day_bounds(day, tz)[0]


def _daily_battery_fields(
    samples: Sequence[tuple[int, Reading]],
) -> tuple[int | None, int | None, int | None, float | None, float | None]:
    """One day's ``soc_min``, ``soc_max``, ``cycles_last``, ``soh_last``
    and ``batt_temp_max`` from its samples (design-data section 4.6).

    ``cycles_last``/``soh_last`` take the chronologically last sample
    that actually reports a value, not simply the physically last
    sample in ``samples`` -- a device that stops reporting one of those
    fields partway through a day must not blank out an otherwise-known
    trend point. ``soc_min``/``soc_max``/``batt_temp_max`` ignore a
    missing reading entirely rather than letting it pull the range
    toward zero (the project's standing NULL-discipline, Named Defect
    "Missing read as zero").
    """
    socs = [reading.soc for _ts, reading in samples if reading.soc is not None]
    temps = [reading.batt_temp_c for _ts, reading in samples if reading.batt_temp_c is not None]
    cycles_last: int | None = None
    soh_last: float | None = None
    for _ts, reading in sorted(samples, key=lambda pair: pair[0]):
        if reading.cycles is not None:
            cycles_last = reading.cycles
        if reading.soh is not None:
            soh_last = reading.soh
    return (
        min(socs) if socs else None,
        max(socs) if socs else None,
        cycles_last,
        soh_last,
        max(temps) if temps else None,
    )


def derive_rollups(
    device_id: int,
    *,
    sample_store: SampleStore,
    rollup_store: RollupStore,
    derivation_store: DerivationStore,
    now: datetime,
    tz: str = "UTC",
    day_fn: Callable[[int], str] | None = None,
    full: bool = False,
) -> bool:
    """Recompute device ``device_id``'s daily rollups: the battery-field
    columns, the energy-field columns (visual-QA batch fix01, fix 6),
    and the grid-field columns (task 20.2).

    Days are bucketed by ``timeutil.local_day`` under ``tz`` (energy
    requirement "Day Boundaries Use a Configurable Local Timezone")
    unless ``day_fn`` overrides it -- the seam this module's previous
    ``_utc_day`` placeholder (Phase 17) existed for, now closed.

    Known gap (same category as apply-progress-batch13's documented
    gaps, not fixed here): on an INCREMENTAL run, the energy window
    queried is ``[lower, now_ts]`` -- the same window the battery
    fields use -- so the very first in-window reading of each counter
    field establishes a fresh baseline with no credited delta, exactly
    like a device's genuine first-ever sample. Any energy that moved
    between the last sample *before* ``lower`` and that first in-window
    sample is therefore not credited to either day. Fixing this would
    need a "last sample at or before a timestamp" query on
    ``storage.samples.SampleStore``, which is outside this batch's
    declared edit surface. A ``full=True`` recompute is unaffected (its
    window starts at the beginning of all stored samples, where this
    same baseline behavior is already correct).

    Starting point: no stored derivation, or a version/parameter change
    -> full recompute from the beginning of all stored samples; a dirty
    mark -> recompute every day from (and including) the whole of
    ``day_fn(dirty_from_ts)`` through now, matching the design's own
    wording ("recomputes days from `local_day(dirty_from_ts)` ...
    through today"); otherwise, already clean -> no-op.

    Each affected day is replaced wholesale (design D4): the whole
    day's samples are re-queried and re-aggregated, never only the
    samples after the dirty mark within that day, so a day's row is
    always self-consistent.

    Returns whether a recompute actually ran. The caller must already
    hold the write transaction `rollup_store` and `derivation_store`
    share (`BEGIN IMMEDIATE`) and commits it once this returns; neither
    store commits on its own (design D4), exactly like
    `outages.service.derive_outages`.
    """
    effective_day_fn: Callable[[int], str] = (
        day_fn if day_fn is not None else partial(local_day, tz=tz)
    )
    params_hash = _params_hash()
    existing = derivation_store.get(device_id, _DERIVATION_NAME)
    needs_full = (
        existing is None
        or existing.version != _ROLLUP_VERSION
        or existing.params_hash != params_hash
    )

    if not full and not needs_full:
        if existing.dirty_from_ts is None:  # type: ignore[union-attr]
            return False
        lower = _day_start(effective_day_fn(existing.dirty_from_ts), tz)  # type: ignore[union-attr]
    else:
        lower = -(2**63)

    now_ts = int(now.timestamp())
    samples_by_day: dict[str, list[tuple[int, Reading]]] = {}
    all_samples: list[tuple[int, Reading]] = []
    for row in sample_store.between(device_id, lower, now_ts):
        samples_by_day.setdefault(effective_day_fn(row.ts), []).append((row.ts, row.reading))
        all_samples.append((row.ts, row.reading))

    # Energy accounting needs the whole queried range at once -- its own
    # counter-delta segmentation can prorate one segment across a day
    # boundary (`energy.accounting.split_by_local_days`), unlike the
    # battery fields above, which only ever look at one day's samples
    # independently. Keyed by the same `local_day(ts, tz)` the
    # `effective_day_fn` default also uses, so both lookups agree on day
    # boundaries whenever no test overrides `day_fn`. `tariff`/`currency`
    # are irrelevant here: only the Wh/flags fields are persisted, never
    # `DailyEnergy.cost`/`currency`.
    energy_by_day = daily_energy_for_samples(all_samples, tz=tz, tariff=None, currency="")

    for day, day_samples in samples_by_day.items():
        soc_min, soc_max, cycles_last, soh_last, batt_temp_max = _daily_battery_fields(day_samples)
        rollup_store.upsert(
            device_id,
            day,
            soc_min=soc_min,
            soc_max=soc_max,
            cycles_last=cycles_last,
            soh_last=soh_last,
            batt_temp_max=batt_temp_max,
        )
        energy = energy_by_day.get(day)
        if energy is not None:
            rollup_store.upsert_energy(
                device_id,
                day,
                chg_ac_wh=energy.chg_ac_wh,
                chg_dc_wh=energy.chg_dc_wh,
                chg_solar_wh=energy.chg_solar_wh,
                dsg_ac_wh=energy.dsg_ac_wh,
                dsg_dc_wh=energy.dsg_dc_wh,
                chg_ac_est_wh=energy.chg_ac_est_wh,
                energy_flags=_encode_energy_flags(energy.flags),
            )
        grid = grid_quality_range(day_samples)
        if grid == "unavailable":
            rollup_store.upsert_grid(
                device_id,
                day,
                grid_v_min=None,
                grid_v_avg=None,
                grid_v_max=None,
                grid_hz_min=None,
                grid_hz_avg=None,
                grid_hz_max=None,
                grid_readings=0,
            )
        else:
            rollup_store.upsert_grid(
                device_id,
                day,
                grid_v_min=grid.grid_v_min,
                grid_v_avg=grid.grid_v_avg,
                grid_v_max=grid.grid_v_max,
                grid_hz_min=grid.grid_hz_min,
                grid_hz_avg=grid.grid_hz_avg,
                grid_hz_max=grid.grid_hz_max,
                grid_readings=grid.readings,
            )

    derivation_store.mark_computed(
        device_id,
        _DERIVATION_NAME,
        version=_ROLLUP_VERSION,
        params_hash=params_hash,
        checkpoint_ts=None,
        computed_at=now_ts,
    )
    return True


__all__ = ["derive_rollups"]
