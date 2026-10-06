"""Unit tests for the outage state machine (`OutageMachine.feed()` and
the batch-equivalent `detect()`).

Both drivers share one implementation (design D6): `detect()` calls the
exact same `feed()` a live collector would call, so the property test at
the end — proving the two agree on a varied, seeded sequence — is really
proving there is only one algorithm, not two that happen to agree today.
"""

from __future__ import annotations

import random

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.outages.detector import OutageMachine, detect
from ecoflow_stats.outages.model import (
    DetectorConfig,
    Ended,
    GapClosed,
    JudgedPoint,
    Quiescent,
    Started,
    judge,
)
from ecoflow_stats.storage.failures import FetchFailure
from ecoflow_stats.storage.runs import AppRun

_CONFIG = DetectorConfig()
_PRESENT_V = 120.0
_ABSENT_V = 0.0


def _reading(grid_v: float | None, **overrides: object) -> Reading:
    base: dict[str, object] = {"soc": 50, "ac_in_w": 500.0, "chg_ac_wh": 0.0}
    base.update(overrides)
    return Reading(grid_v=grid_v, **base)


def test_three_consecutive_below_floor_readings_record_an_official_outage() -> None:
    """Scenario "Two or more consecutive below-floor readings count as an
    official outage": a detector that required 3+ readings, or counted
    them as merely "brief", would undercount a real outage."""
    samples = [
        (0, _reading(_PRESENT_V, soc=51)),
        (60, _reading(_ABSENT_V, soc=50)),
        (120, _reading(_ABSENT_V, soc=49)),
        (180, _reading(_ABSENT_V, soc=48)),
        (240, _reading(_PRESENT_V, soc=48)),
    ]

    result = detect(samples, config=_CONFIG)

    assert len(result.events) == 1
    event = result.events[0]
    assert event.kind == "outage"
    assert event.start_ts == 60
    assert event.end_ts == 240
    assert event.readings == 3


def test_exactly_two_consecutive_below_floor_readings_still_count_as_official() -> None:
    """Scenario "Exactly two consecutive readings — the shortest real
    outage duration observed in imported history — still counts": the
    debounce must confirm at `confirm_readings`, not require more."""
    samples = [
        (0, _reading(_PRESENT_V)),
        (60, _reading(_ABSENT_V)),
        (120, _reading(_ABSENT_V)),
        (180, _reading(_PRESENT_V)),
    ]

    result = detect(samples, config=_CONFIG)

    assert len(result.events) == 1
    assert result.events[0].kind == "outage"
    assert result.events[0].readings == 2


def test_a_single_isolated_below_floor_reading_is_a_brief_event_not_official() -> None:
    """Scenario "A single isolated below-floor reading is a brief event,
    not an official outage": it must still be recorded (never silently
    dropped), just not counted as official."""
    samples = [
        (0, _reading(_PRESENT_V)),
        (60, _reading(_ABSENT_V)),
        (120, _reading(_PRESENT_V)),
    ]

    result = detect(samples, config=_CONFIG)

    assert len(result.events) == 1
    assert result.events[0].kind == "brief"
    assert result.events[0].readings == 1


def test_no_below_floor_reading_means_nothing_recorded() -> None:
    """Scenario "No below-floor reading means nothing recorded": an
    all-present period must not fabricate an event or a gap. SoC varies
    slightly per reading (as a real device's would) so every sample is
    judged on the voltage threshold itself, not incidentally shortcut by
    the stale-payload guard (amendment A3)."""
    samples = [
        (t, _reading(_PRESENT_V, soc=soc)) for t, soc in ((0, 60), (60, 59), (120, 58), (180, 57))
    ]

    result = detect(samples, config=_CONFIG)

    assert result.events == []
    assert result.gaps == []


def test_a_gap_caused_by_a_collection_failure_is_not_an_outage() -> None:
    """Scenario "A gap caused by a collection failure is not an outage"
    (live-feed half) — Named Defect "Gap as outage". Grid present on both
    sides, as in the real 2026-10-03 API-failure gaps (verified-premises
    obs 5697): the detector must record a gap, never fabricate an outage
    from the silence."""
    machine = OutageMachine(config=_CONFIG)
    present = _reading(_PRESENT_V)
    run = AppRun(id=1, started_at=0, last_tick_at=240, stopped_at=None, app_version="0.1.0")
    failures = [
        FetchFailure(ts=120, outcome="network", code=None, attempts=2, latency_ms=900),
        FetchFailure(ts=180, outcome="network", code=None, attempts=2, latency_ms=900),
    ]

    first = machine.feed(0, present, judge(present, [], config=_CONFIG))
    second = machine.feed(60, present, judge(present, [], config=_CONFIG))
    third = machine.feed(
        240, present, judge(present, [], config=_CONFIG), failures=failures, app_runs=[run]
    )

    assert machine.event is None
    assert not any(isinstance(t, Started) for t in first + second + third)
    gap_transitions = [t for t in third if isinstance(t, GapClosed)]
    assert len(gap_transitions) == 1
    gap = gap_transitions[0].gap
    assert gap.start_ts == 60
    assert gap.end_ts == 240
    assert gap.cause == "network"


def test_live_feed_and_batch_detect_agree_on_a_seeded_random_sequence() -> None:
    """Named Defect "Live/batch disagreement": design D6 is that ONE
    detector serves both live alerts and batch recomputation, so they
    must never disagree about whether a period was an outage. This test
    writes its own independent "live" driving loop (not calling
    `detect()`), so a future `detect()` that stops routing through the
    same `OutageMachine.feed()` the live collector uses — for example, a
    rewritten batch loop that forgets to carry the staleness window
    across a gap, or drops the still-open trailing event — would make
    the two diverge and this test would catch it."""
    rng = random.Random(20261005)
    samples: list[tuple[int, Reading]] = []
    for i in range(200):
        ts = i * 60
        grid_v = rng.choice([0.0, 30.0, 120.0, 121.5, None])
        samples.append((ts, _reading(grid_v)))

    live_machine = OutageMachine(config=_CONFIG)
    live_window: list[Reading] = []
    live_events = []
    for ts, reading in samples:
        live_judgment = judge(reading, live_window, config=_CONFIG)
        for transition in live_machine.feed(ts, reading, live_judgment):
            if isinstance(transition, Ended):
                live_events.append(transition.event)
        if live_judgment.state != "unjudged":
            live_window.append(reading)
            if len(live_window) > _CONFIG.stale_repeat - 1:
                live_window.pop(0)
    if live_machine.event is not None:
        live_events.append(live_machine.event)

    batch_result = detect(samples, config=_CONFIG)

    assert batch_result.events == live_events


def test_incremental_detect_resuming_from_a_quiescent_checkpoint_matches_a_full_recompute() -> None:
    """Named Defect "Incremental drift": `outages.service.derive_outages`
    resumes `detect()` from the last `Quiescent` checkpoint instead of
    replaying the whole history. If the resume path (`OutageMachine.
    resumed_at` or `detect`'s `resume_from` handling) ever diverged from
    feeding the same tail of samples through a cold machine, a recompute
    after a detector version bump or a late import would silently drift
    from what a full rebuild produces — this proves every event and gap
    at or after a real mid-sequence checkpoint is byte-identical either
    way, not just that the two happen to agree by coincidence at the very
    end of the sequence (which the live/batch test above already covers)."""
    rng = random.Random(20261006)
    samples: list[tuple[int, Reading]] = []
    for i in range(300):
        ts = i * 60
        grid_v = rng.choice([0.0, 30.0, 120.0, 121.5])
        # SoC ticks down every sample (never repeating) so no run of
        # identical readings ever trips the stale-payload guard (amendment
        # A3) at an arbitrary point -- that guard's own cold-window
        # behavior on resume is a separate, already-documented
        # simplification (`OutageMachine.resumed_at`), not what this test
        # is about.
        samples.append((ts, _reading(grid_v, soc=1 + (i % 99))))

    full = detect(samples, config=_CONFIG)

    # Walk the sequence by hand to recover every intermediate `Quiescent`
    # checkpoint — `detect()` itself only exposes the very last one.
    machine = OutageMachine(config=_CONFIG)
    window: list[Reading] = []
    checkpoints: list[int] = []
    for ts, reading in samples:
        judgment = judge(reading, window, config=_CONFIG)
        for transition in machine.feed(ts, reading, judgment):
            if isinstance(transition, Quiescent):
                checkpoints.append(transition.ts)
        if judgment.state != "unjudged":
            window.append(reading)
            if len(window) > _CONFIG.stale_repeat - 1:
                window.pop(0)
    assert len(checkpoints) > 3, "the seeded sequence must offer several real resume points"
    checkpoint_ts = checkpoints[len(checkpoints) // 2]
    checkpoint_reading = next(reading for ts, reading in samples if ts == checkpoint_ts)
    resume_point = JudgedPoint(
        ts=checkpoint_ts,
        state="present",
        soc=checkpoint_reading.soc,
        chg_ac_wh=checkpoint_reading.chg_ac_wh,
        ac_in_w=checkpoint_reading.ac_in_w,
    )
    tail = [(ts, reading) for ts, reading in samples if ts > checkpoint_ts]

    incremental = detect(tail, config=_CONFIG, resume_from=resume_point)

    expected_events = [e for e in full.events if e.start_ts >= checkpoint_ts]
    expected_gaps = [g for g in full.gaps if g.start_ts >= checkpoint_ts]
    assert incremental.events == expected_events
    assert incremental.gaps == expected_gaps
