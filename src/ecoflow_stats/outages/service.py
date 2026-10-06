"""Outage recompute orchestration (design-data section 4.3).

Decides whether a device's outages can resume from the last `Quiescent`
checkpoint or need a full recompute, then replays the shared
`OutageMachine` over stored samples to produce the versioned, derived
`outage_events` and `gaps` rows.

Pure application layer: depends only on the structural `Protocol`s in
`ports.py`, never on a concrete `storage` module or `sqlite3` directly, so
this module stays part of the pure core
(`tests/contract/test_pure_core_imports.py`). The caller owns the
surrounding transaction -- `OutageStore.replace_from` and
`DerivationStore.mark_computed` never commit on their own (design D4: one
`BEGIN IMMEDIATE` transaction per derivation run).
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING

from ecoflow_stats.outages import model
from ecoflow_stats.outages.detector import detect
from ecoflow_stats.outages.model import DetectorConfig, JudgedPoint

if TYPE_CHECKING:
    from datetime import datetime

    from ecoflow_stats.ports import DerivationStore, FailureLog, OutageStore, RunLog, SampleStore

_DERIVATION_NAME = "outages"
_DEFAULT_CONFIG = DetectorConfig()


def _params_hash(config: DetectorConfig) -> str:
    """A short fingerprint of the thresholds that change a recompute's
    output, so a configuration change forces a full recompute exactly
    like a detector version bump does (design-data section 1)."""
    raw = (
        f"{config.threshold_v}:{config.confirm_readings}:"
        f"{config.gap_threshold_s}:{config.stale_repeat}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def derive_outages(
    device_id: int,
    *,
    sample_store: SampleStore,
    failure_log: FailureLog,
    run_log: RunLog,
    outage_store: OutageStore,
    derivation_store: DerivationStore,
    config: DetectorConfig = _DEFAULT_CONFIG,
    now: datetime,
    full: bool = False,
) -> bool:
    """Recompute device `device_id`'s outages, incrementally when the
    stored derivation allows it.

    Starting point:
    - no stored derivation, a detector version or parameter change, or no
      usable checkpoint -> full recompute from the beginning;
    - the stored checkpoint is itself after the dirty mark (an import
      backfilled history older than what was already computed) -> full
      recompute, since there is no known-good checkpoint at or before
      that earlier point;
    - otherwise, and only when dirty -> resume from the stored checkpoint.

    Returns whether a recompute actually ran. `False` only when the
    derivation was already clean (`dirty_from_ts is None`) and `full` was
    not requested -- the common case on most collector-interval ticks.

    The caller must already hold the write transaction `outage_store` and
    `derivation_store` share (`BEGIN IMMEDIATE`) and commits it once this
    returns; neither store commits on its own (design D4).
    """
    params_hash = _params_hash(config)
    existing = derivation_store.get(device_id, _DERIVATION_NAME)

    needs_full = (
        existing is None
        or existing.version != model.DETECTOR_VERSION
        or existing.params_hash != params_hash
        or existing.checkpoint_ts is None
        or (existing.dirty_from_ts is not None and existing.checkpoint_ts > existing.dirty_from_ts)
    )
    if not full and not needs_full and existing is not None and existing.dirty_from_ts is None:
        return False

    start: int | None = None if (full or needs_full) else existing.checkpoint_ts  # type: ignore[union-attr]
    now_ts = int(now.timestamp())

    resume_from: JudgedPoint | None = None
    if start is not None:
        checkpoint_rows = list(sample_store.between(device_id, start, start))
        if checkpoint_rows:
            checkpoint_reading = checkpoint_rows[0].reading
            resume_from = JudgedPoint(
                ts=start,
                state="present",
                soc=checkpoint_reading.soc,
                chg_ac_wh=checkpoint_reading.chg_ac_wh,
                ac_in_w=checkpoint_reading.ac_in_w,
            )
        else:
            # The checkpoint sample is gone (should not happen in normal
            # operation -- samples are immutable); fall back to a full
            # recompute rather than resuming from an unverifiable point.
            start = None

    lower = (start + 1) if start is not None else -(2**63)
    samples = [(row.ts, row.reading) for row in sample_store.between(device_id, lower, now_ts)]
    failures = failure_log.between(device_id, lower, now_ts)
    app_runs = run_log.covering(lower, now_ts)

    result = detect(
        samples, failures=failures, app_runs=app_runs, config=config, resume_from=resume_from
    )

    # `Event`/`Gap`'s own `detector_version` dataclass field defaults to
    # whatever `DETECTOR_VERSION` was when `outages.model` was first
    # imported in this process -- a class-body default is bound once, not
    # a live read of the module attribute. The orchestration layer (here)
    # is what actually knows "the version this run computed under", so it
    # owns stamping every stored row with it explicitly, rather than
    # trusting a frozen default to track a version that can change within
    # one process's lifetime between a detector upgrade and its rollback.
    current_version = model.DETECTOR_VERSION
    for event in result.events:
        event.detector_version = current_version
    stamped_gaps = [
        dataclasses.replace(gap, detector_version=current_version) for gap in result.gaps
    ]

    outage_store.replace_from(device_id, start, result.events, stamped_gaps)
    new_checkpoint = result.checkpoint_ts if result.checkpoint_ts is not None else start
    derivation_store.mark_computed(
        device_id,
        _DERIVATION_NAME,
        version=current_version,
        params_hash=params_hash,
        checkpoint_ts=new_checkpoint,
        computed_at=now_ts,
    )
    return True


__all__ = ["derive_outages"]
