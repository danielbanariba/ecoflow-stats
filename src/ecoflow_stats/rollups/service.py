"""Daily rollup recompute orchestration (design-data section 4.8).

Decides whether a device's `daily_rollups` can resume from the dirty
day onward or need a full recompute, then re-aggregates each affected
day's stored samples into this slice's battery-field columns
(`cycles_last`, `soh_last`, `soc_min`/`max`, `batt_temp_max`). Energy
and grid fields extend this module's per-day aggregation in Phases
18/20 -- see the module docstring's "additively extensible" note in
`storage.rollups`.

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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.ports import DerivationStore, SampleStore
    from ecoflow_stats.storage.rollups import RollupStore

_DERIVATION_NAME = "rollups"
_ROLLUP_VERSION = 1


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


def _utc_day(ts: int) -> str:
    """UTC calendar day for ``ts``, as ``'YYYY-MM-DD'``.

    A placeholder day-bucketing rule for this slice only. Design-data
    section 4.8 buckets rollup days through a `timeutil.local_day`
    helper that does not exist yet (Phase 18, slice 28) -- that phase
    buckets by `ECOFLOW_STATS_TZ` via `zoneinfo` instead, including
    proration across a DST transition, and is expected to pass its own
    `local_day` in as `day_fn` below rather than needing this module
    rewritten. Until then, every day boundary in this module is UTC.
    """
    return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()


def _utc_day_start(day: str) -> int:
    """The UTC epoch second at which ``day`` (a ``'YYYY-MM-DD'`` string
    produced by a ``day_fn`` such as `_utc_day`) begins -- used only to
    widen an incremental recompute's query to the whole of its dirty
    day, never to enumerate days that have no samples."""
    year, month, day_of_month = (int(part) for part in day.split("-"))
    return int(datetime(year, month, day_of_month, tzinfo=UTC).timestamp())


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
    day_fn: Callable[[int], str] = _utc_day,
    full: bool = False,
) -> bool:
    """Recompute device ``device_id``'s daily rollups -- today, this
    slice's battery-field columns only.

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
        lower = _utc_day_start(day_fn(existing.dirty_from_ts))  # type: ignore[union-attr]
    else:
        lower = -(2**63)

    now_ts = int(now.timestamp())
    samples_by_day: dict[str, list[tuple[int, Reading]]] = {}
    for row in sample_store.between(device_id, lower, now_ts):
        samples_by_day.setdefault(day_fn(row.ts), []).append((row.ts, row.reading))

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
