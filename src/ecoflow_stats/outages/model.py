"""The outage detector's shared constants, the pure per-sample
grid-presence judgment, and the value types `detector.py`/`evidence.py`
build and exchange — both the live collector and batch recomputation use
the same ones (design D6: one detector serves both, so they never
disagree).

`judge()` never sees a raw device payload — it is given an already-
normalized `Reading`, so a `Reading`'s own dataclass equality already
means "the whole measurement row matches," with no extra field-listing
needed to implement the whole-row staleness comparison (amendment A3).

`FailureLike`/`AppRunLike` are structural stand-ins for
`storage.failures.FetchFailure`/`storage.runs.AppRun`: this package is
pure core and must never import `storage` (Named Defect "Core doing
I/O"; a static AST scan in `tests/contract/test_pure_core_imports.py`
enforces it even inside a `TYPE_CHECKING` block), so gap evidence
describes only the shape of a failure/run it actually needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


class FailureLike(Protocol):
    """The shape `evidence.make_gap` needs from one fetch failure,
    matching `storage.failures.FetchFailure` structurally — without
    importing it."""

    ts: int
    outcome: str
    code: str | None


class AppRunLike(Protocol):
    """The shape `evidence.make_gap` needs from one application run,
    matching `storage.runs.AppRun` structurally — without importing it."""

    started_at: int
    last_tick_at: int | None
    stopped_at: int | None


@dataclass(frozen=True, slots=True)
class JudgedPoint:
    """A judged (never unjudged) reading's minimal remembered shape: what
    the state machine and gap evidence need about "the last/next judged
    reading" without holding the full `Reading` or re-deriving its state
    later."""

    ts: int
    state: Literal["present", "absent"]
    soc: int | None
    chg_ac_wh: float | None
    ac_in_w: float | None


@dataclass(slots=True)
class Event:
    """One outage (or brief) event.

    Mutable by design: `OutageMachine.feed()` progressively builds one of
    these across several calls (incrementing `readings`, tracking
    `soc_min`, upgrading `kind` once confirmed) exactly as the design's
    state-machine pseudocode does — the alternative, replacing it with a
    fresh immutable copy on every reading, would not change behavior,
    only add noise. Shape matches the persisted `outage_events` row,
    minus the identity/device columns the storage layer owns.
    """

    start_ts: int
    kind: Literal["outage", "brief"] = "brief"
    readings: int = 1
    end_ts: int | None = None
    start_uncertainty_s: int | None = None
    end_uncertainty_s: int | None = None
    start_in_gap: bool = False
    end_in_gap: bool = False
    soc_start: int | None = None
    soc_end: int | None = None
    soc_min: int | None = None
    dsg_remain_min_start: int | None = None
    legacy_id: int | None = None
    detector_version: int = DETECTOR_VERSION


@dataclass(frozen=True, slots=True)
class Gap:
    """One silent window between two judged readings — never an outage by
    inference, but recorded with its cause and before/after evidence so a
    user can decide (design D5; outages requirement "A Data Gap Is Never
    Classified as an Outage")."""

    start_ts: int
    end_ts: int | None
    cause: str
    state_before: Literal["present", "absent"] | None
    state_after: Literal["present", "absent"] | None
    soc_before: int | None
    soc_after: int | None
    chg_ac_wh_delta: float | None
    expected_in_wh: float | None
    evidence: Literal["likely_present", "likely_absent", "inconclusive"]
    failures: Mapping[str, int]
    detector_version: int = DETECTOR_VERSION


@dataclass(frozen=True, slots=True)
class Started:
    """A new below-floor event just began (`readings == 1`): a live
    caller alerts immediately, before the debounce confirms it official
    (notifications requirement: alert on the first below-threshold
    reading)."""

    event: Event


@dataclass(frozen=True, slots=True)
class Confirmed:
    """An open event just reached `confirm_readings`: it is now an
    official outage, not merely brief."""

    event: Event


@dataclass(frozen=True, slots=True)
class Ended:
    """An open event just closed, at the first above-floor judged reading
    after it."""

    event: Event


@dataclass(frozen=True, slots=True)
class GapClosed:
    """A silent window just closed, at the first judged reading after a
    silence longer than `gap_threshold_s`."""

    gap: Gap


@dataclass(frozen=True, slots=True)
class Quiescent:
    """A judged present reading outside any gap: the canonical safe
    restart checkpoint an incremental recompute can resume from
    (design-data section 4.3)."""

    ts: int


Transition = Started | Confirmed | Ended | GapClosed | Quiescent
"""Every kind of event `OutageMachine.feed()` can report back for one
sample: a caller (live notifications, batch persistence) matches on the
concrete type it cares about and ignores the rest."""


__all__ = [
    "ABSENT",
    "DETECTOR_VERSION",
    "PRESENT",
    "UNJUDGED",
    "AppRunLike",
    "Confirmed",
    "DetectorConfig",
    "Ended",
    "Event",
    "FailureLike",
    "Gap",
    "GapClosed",
    "JudgedPoint",
    "Judgment",
    "Quiescent",
    "Started",
    "Transition",
    "judge",
]
