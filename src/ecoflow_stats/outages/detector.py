"""The outage state machine: one implementation that serves both the
live collector and batch recomputation (design D6), so the two can never
disagree about whether a given period was an outage.

`OutageMachine.feed()` takes one already-judged reading at a time and is
the only place an `Event` or `Gap` is built or mutated. `detect()` is the
batch-equivalent convenience that feeds a whole, already-ordered sample
sequence through a fresh machine and collects the result — the same
`feed()` a live caller would call, never a separate algorithm, which is
exactly what makes the two agree (Named Defect "Live/batch disagreement").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ecoflow_stats.outages.evidence import make_gap
from ecoflow_stats.outages.model import (
    Confirmed,
    DetectorConfig,
    Ended,
    Event,
    Gap,
    GapClosed,
    JudgedPoint,
    Quiescent,
    Started,
    judge,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.outages.model import AppRunLike, FailureLike, Judgment, Transition

_DEFAULT_CONFIG = DetectorConfig()


def _min_ignoring_none(current: int | None, candidate: int | None) -> int | None:
    """`min()`, but a missing value on either side never wins — a sample
    with no SoC must not look like "charge dropped to nothing" (Named
    Defect "Missing read as zero")."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return min(current, candidate)


class OutageMachine:
    """Stateful, pure outage detector: no I/O, but it genuinely mutates
    its own state across `feed()` calls — `mode`, the open `event`,
    `pending` unjudged reasons, and `last_judged` are exactly what let a
    live-fed sequence and a batch-fed sequence agree (design D6)."""

    def __init__(self, *, config: DetectorConfig = _DEFAULT_CONFIG) -> None:
        self.config = config
        self.mode: str = "unknown"
        """`"present"`, `"absent"`, or `"unknown"` before the first judged
        reading."""
        self.event: Event | None = None
        self.last_judged: JudgedPoint | None = None
        self.pending: dict[str, int] = {}
        self._last_below_ts: int | None = None

    @classmethod
    def resumed_at(
        cls, checkpoint: JudgedPoint, *, config: DetectorConfig = _DEFAULT_CONFIG
    ) -> OutageMachine:
        """Build a machine as if it had just reached `checkpoint` live:
        the canonical `Quiescent` restart point an incremental recompute
        (or the live collector's own startup replay) can resume from
        without rebuilding the entire history (design-data section 4.3).

        A `Quiescent` checkpoint is only ever emitted while `mode ==
        "present"`, outside any gap and with no open event -- exactly
        the state this constructs. The staleness-detection window itself
        starts empty either way (matching the live driver's own replay),
        so the first one or two samples fed after resuming have reduced
        power to catch a stale payload that straddles the checkpoint --
        an accepted, bounded simplification shared by both drivers.
        """
        machine = cls(config=config)
        machine.mode = "present"
        machine.last_judged = checkpoint
        return machine

    def feed(
        self,
        ts: int,
        reading: Reading,
        judgment: Judgment,
        *,
        failures: Sequence[FailureLike] = (),
        app_runs: Sequence[AppRunLike] = (),
    ) -> list[Transition]:
        """Judge one more reading into the machine; return every
        transition it caused, in order. `judgment` is supplied by the
        caller (already computed via `judge()` over its own sliding
        window), never recomputed here."""
        if judgment.state == "unjudged":
            self.pending[judgment.reason] = self.pending.get(judgment.reason, 0) + 1
            return []

        transitions: list[Transition] = []
        in_gap = False
        if self.last_judged is not None and ts - self.last_judged.ts > self.config.gap_threshold_s:
            gap = make_gap(
                self.last_judged,
                JudgedPoint(
                    ts=ts,
                    state=judgment.state,
                    soc=reading.soc,
                    chg_ac_wh=reading.chg_ac_wh,
                    ac_in_w=reading.ac_in_w,
                ),
                failures=failures,
                pending=dict(self.pending),
                app_runs=app_runs,
            )
            transitions.append(GapClosed(gap))
            in_gap = True

        if judgment.state == "absent":
            if self.mode != "absent":
                started_from_unknown = self.last_judged is None
                event = Event(
                    start_ts=ts,
                    start_uncertainty_s=(ts - self.last_judged.ts) if self.last_judged else None,
                    start_in_gap=in_gap or started_from_unknown,
                    soc_start=reading.soc
                    if reading.soc is not None
                    else (self.last_judged.soc if self.last_judged else None),
                    soc_min=reading.soc,
                    dsg_remain_min_start=reading.dsg_remain_min,
                )
                self.event = event
                transitions.append(Started(event))
            else:
                event = self.event
                assert event is not None  # mode == "absent" implies an open event
                event.readings += 1
                event.soc_min = _min_ignoring_none(event.soc_min, reading.soc)
                if event.readings == self.config.confirm_readings:
                    event.kind = "outage"
                    transitions.append(Confirmed(event))
            self._last_below_ts = ts
            self.mode = "absent"
        else:  # judgment.state == "present"
            if self.mode == "absent":
                event = self.event
                assert event is not None
                assert self._last_below_ts is not None
                event.end_ts = ts
                event.end_uncertainty_s = ts - self._last_below_ts
                event.end_in_gap = in_gap
                event.soc_end = reading.soc
                transitions.append(Ended(event))
                self.event = None
            self.mode = "present"
            if not in_gap:
                transitions.append(Quiescent(ts))

        self.last_judged = JudgedPoint(
            ts=ts,
            state=judgment.state,
            soc=reading.soc,
            chg_ac_wh=reading.chg_ac_wh,
            ac_in_w=reading.ac_in_w,
        )
        self.pending.clear()
        return transitions


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """The outcome of one `detect()` batch run."""

    events: list[Event] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    checkpoint_ts: int | None = None
    """The last `Quiescent` timestamp seen — the safe point an
    incremental recompute can resume from (design-data section 4.3)."""


def detect(
    samples: Sequence[tuple[int, Reading]],
    *,
    failures: Sequence[FailureLike] = (),
    app_runs: Sequence[AppRunLike] = (),
    config: DetectorConfig = _DEFAULT_CONFIG,
    resume_from: JudgedPoint | None = None,
) -> DetectionResult:
    """Feed a whole, already-ts-ordered sample sequence through one
    `OutageMachine` and collect the final events and gaps — the batch
    equivalent of calling `machine.feed()` once per live sample.

    Maintains its own sliding window of judged readings to call `judge()`
    exactly as a live caller must (`judge`'s own contract: the caller
    supplies the last `config.stale_repeat - 1` samples).

    With `resume_from` given, the machine starts at that checkpoint
    instead of cold (`OutageMachine.resumed_at`) and `samples` is expected
    to hold only what comes strictly after it — the incremental-recompute
    path (`outages.service.derive_outages`). This is still the one
    `feed()` loop a fresh `detect()` call uses, so an incremental run and
    a full run agree on every event and gap at or after the checkpoint
    (Named Defect "Incremental drift").
    """
    machine = (
        OutageMachine.resumed_at(resume_from, config=config)
        if resume_from is not None
        else OutageMachine(config=config)
    )
    window: list[Reading] = []
    events: list[Event] = []
    gaps: list[Gap] = []
    checkpoint_ts: int | None = resume_from.ts if resume_from is not None else None
    for ts, reading in samples:
        judgment = judge(reading, window, config=config)
        transitions = machine.feed(ts, reading, judgment, failures=failures, app_runs=app_runs)
        for transition in transitions:
            if isinstance(transition, Ended):
                events.append(transition.event)
            elif isinstance(transition, GapClosed):
                gaps.append(transition.gap)
            elif isinstance(transition, Quiescent):
                checkpoint_ts = transition.ts
        if judgment.state != "unjudged":
            window.append(reading)
            excess = len(window) - (config.stale_repeat - 1)
            if excess > 0:
                del window[:excess]
    if machine.event is not None:
        events.append(machine.event)
    return DetectionResult(events=events, gaps=gaps, checkpoint_ts=checkpoint_ts)


@dataclass
class LiveOutageState:
    """One device's live-tracking state, carried across collector ticks:
    the `OutageMachine` instance and the sliding `judge()` window it
    needs. `feed()` is the one call both a live collector tick and this
    module's own startup replay (`build_live_state` below) use to judge
    and advance a single new reading, so they can never compute the
    judgment differently (design D6: one detector serves both live
    alerts and recomputed statistics)."""

    machine: OutageMachine
    window: list[Reading] = field(default_factory=list)

    def feed(self, ts: int, reading: Reading, *, config: DetectorConfig) -> list[Transition]:
        """Judge `reading` against this state's trailing window, feed it
        into the machine, and advance the window."""
        judgment = judge(reading, self.window, config=config)
        transitions = self.machine.feed(ts, reading, judgment)
        if judgment.state != "unjudged":
            self.window.append(reading)
            excess = len(self.window) - (config.stale_repeat - 1)
            if excess > 0:
                del self.window[:excess]
        return transitions


def build_live_state(
    samples: Sequence[tuple[int, Reading]],
    *,
    config: DetectorConfig = _DEFAULT_CONFIG,
    resume_from: JudgedPoint | None = None,
) -> LiveOutageState:
    """Replay `samples` (already ts-ordered, strictly after `resume_from`
    when given) through a fresh machine with every transition discarded,
    and return the resulting live state -- the live collector's own
    startup replay (design D6: "rebuilt at startup by replaying from the
    last quiescent checkpoint with notifications disabled"), so
    restarting mid-outage never re-fires a `Started` alert for an event
    that already began before the restart (notifications requirement:
    "No Duplicate Notifications Across Restarts"). The caller
    (`outages.service.build_live_outage_state`) is responsible for
    resolving `resume_from` and supplying only the samples after it.
    """
    machine = (
        OutageMachine.resumed_at(resume_from, config=config)
        if resume_from is not None
        else OutageMachine(config=config)
    )
    state = LiveOutageState(machine=machine)
    for ts, reading in samples:
        state.feed(ts, reading, config=config)
    return state


__all__ = ["DetectionResult", "LiveOutageState", "OutageMachine", "build_live_state", "detect"]
