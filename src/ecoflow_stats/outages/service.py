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
from typing import TYPE_CHECKING, Literal

from ecoflow_stats.outages import model
from ecoflow_stats.outages.detector import build_live_state, detect
from ecoflow_stats.outages.model import Decision, DetectorConfig, JudgedPoint

if TYPE_CHECKING:
    from datetime import datetime

    from ecoflow_stats.outages.detector import LiveOutageState
    from ecoflow_stats.ports import (
        DecisionStore,
        DerivationStore,
        FailureLog,
        OutageStore,
        RunLog,
        SampleStore,
    )

_DERIVATION_NAME = "outages"
_DEFAULT_CONFIG = DetectorConfig()
_VALID_VERDICTS: dict[str, frozenset[str]] = {
    "gap": frozenset({"outage", "no_outage"}),
    "legacy": frozenset({"real", "phantom"}),
}


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


def record_decision(
    device_id: int,
    *,
    target: Literal["gap", "legacy"],
    start_ts: int,
    end_ts: int,
    verdict: Literal["outage", "no_outage", "real", "phantom"],
    decision_store: DecisionStore,
    now: datetime,
) -> int:
    """Record the user's verdict on a gap or a legacy outage event,
    anchored to its own time interval so it survives every future
    recompute (outages requirement: "A User Decision on a Gap Is
    Durable and Survives Recomputation"; `derive_outages` above never
    reads or writes `decisions` at all, so nothing here needs to
    coordinate with it beyond sharing the same device).

    Raises `ValueError` for a verdict that makes no sense for its
    target (a `target="gap"` only ever resolves to `outage`/`no_outage`;
    `target="legacy"` only ever to `real`/`phantom`) -- a mismatch is a
    caller bug, not a legitimate choice `resolve()` would ever need to
    handle.
    """
    valid = _VALID_VERDICTS[target]
    if verdict not in valid:
        raise ValueError(
            f"verdict {verdict!r} is not valid for target {target!r} (expected one of {sorted(valid)})"
        )
    decision = Decision(
        device_id=device_id,
        target=target,
        start_ts=start_ts,
        end_ts=end_ts,
        verdict=verdict,
        decided_at=int(now.timestamp()),
    )
    return decision_store.add(decision)


def undo_decision(decision_id: int, *, decision_store: DecisionStore) -> None:
    """Undo a previously recorded decision: it stops being active, but
    stays in the audit trail, superseded by itself rather than deleted."""
    decision_store.undo(decision_id)


def build_live_outage_state(
    device_id: int,
    *,
    sample_store: SampleStore,
    derivation_store: DerivationStore,
    config: DetectorConfig = _DEFAULT_CONFIG,
    now: datetime,
) -> LiveOutageState:
    """Rebuild device `device_id`'s live `OutageMachine` by replaying
    stored samples from its last known-good `outages` checkpoint (or
    from the beginning, when none exists yet) through `now`, with every
    transition discarded -- the live collector's own startup replay
    (design D6: "rebuilt at startup by replaying from the last quiescent
    checkpoint with notifications disabled"), so restarting mid-outage
    never re-fires a `Started` alert for an event that already began
    before the restart (notifications requirement: "No Duplicate
    Notifications Across Restarts").

    Deliberately simpler than `derive_outages`'s own resume logic above:
    a stale or version-mismatched checkpoint here only costs one extra
    full replay (cheap, and only happens once at startup), never a
    wrong persisted row, so this never needs `derive_outages`'s own
    needs-full-recompute reasoning about a detector version or
    parameter change.
    """
    existing = derivation_store.get(device_id, _DERIVATION_NAME)
    resume_from: JudgedPoint | None = None
    if existing is not None and existing.checkpoint_ts is not None:
        checkpoint_rows = list(
            sample_store.between(device_id, existing.checkpoint_ts, existing.checkpoint_ts)
        )
        if checkpoint_rows:
            checkpoint_reading = checkpoint_rows[0].reading
            resume_from = JudgedPoint(
                ts=existing.checkpoint_ts,
                state="present",
                soc=checkpoint_reading.soc,
                chg_ac_wh=checkpoint_reading.chg_ac_wh,
                ac_in_w=checkpoint_reading.ac_in_w,
            )
    lower = (resume_from.ts + 1) if resume_from is not None else -(2**63)
    now_ts = int(now.timestamp())
    samples = [(row.ts, row.reading) for row in sample_store.between(device_id, lower, now_ts)]
    return build_live_state(samples, config=config, resume_from=resume_from)


__all__ = ["build_live_outage_state", "derive_outages", "record_decision", "undo_decision"]
