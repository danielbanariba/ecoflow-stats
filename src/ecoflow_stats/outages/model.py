"""The outage detector's shared constants and the pure per-sample
grid-presence judgment both the live collector and batch recomputation
use (design D6: one detector serves both, so they never disagree).

`judge()` never sees a raw device payload — it is given an already-
normalized `Reading`, so a `Reading`'s own dataclass equality already
means "the whole measurement row matches," with no extra field-listing
needed to implement the whole-row staleness comparison (amendment A3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading

DETECTOR_VERSION = 1
"""Bumped when the judge/state-machine algorithm itself changes — recorded
on every derived outage event and gap so a recompute can tell which
version produced them (storage requirement: outage recomputation is
versioned)."""


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Runtime-configurable detector thresholds (design-data section 4;
    defaults match the legacy watcher's own tuning)."""

    threshold_v: float = 50.0
    """Panel `VOLT_FLOOR = 50_000` mV, in volts: grid voltage at or below
    this reads as absent."""
    confirm_readings: int = 2
    """Consecutive below-floor judged readings needed for an "official"
    outage; fewer is a "brief" event."""
    gap_threshold_s: int = 150
    """Seconds between judged readings beyond which the silence is a gap,
    never inferred as an outage."""
    stale_repeat: int = 3
    """The Nth consecutive identical measurement row onward is judged
    `UNJUDGED('stale_payload')` — prospective, so live and batch agree."""


@dataclass(frozen=True, slots=True)
class Judgment:
    """The grid-presence verdict for one sample.

    Never a plain boolean: a sample lacking `grid_v`, or a repeated cached
    payload, must stay `UNJUDGED` rather than default to either state
    (amendment A2) — an unjudged sample must never start or end an outage.
    """

    state: Literal["present", "absent", "unjudged"]
    reason: str | None = None


PRESENT = Judgment(state="present")
ABSENT = Judgment(state="absent")
_DEFAULT_CONFIG = DetectorConfig()


def UNJUDGED(reason: str) -> Judgment:
    """Build an `UNJUDGED` judgment with the given reason."""
    return Judgment(state="unjudged", reason=reason)


def judge(
    sample: Reading,
    previous: Sequence[Reading],
    *,
    config: DetectorConfig = _DEFAULT_CONFIG,
) -> Judgment:
    """Judge one sample's grid presence, pure and side-effect-free.

    `previous` is the sliding window of the immediately preceding samples,
    in order — the caller (the live collector or a batch recompute) is
    responsible for passing exactly the last `config.stale_repeat - 1`
    samples; fewer (for example, at the very start of a sequence) simply
    cannot trigger the stale-payload guard yet.
    """
    if sample.grid_v is None:
        return UNJUDGED("unjudgeable")
    if len(previous) == config.stale_repeat - 1 and all(sample == p for p in previous):
        return UNJUDGED("stale_payload")
    return PRESENT if sample.grid_v > config.threshold_v else ABSENT


__all__ = [
    "ABSENT",
    "DETECTOR_VERSION",
    "PRESENT",
    "UNJUDGED",
    "DetectorConfig",
    "Judgment",
    "judge",
]
