"""Unit tests for :class:`SupervisedTask`, the task-restart-with-backoff
skeleton every long-running job (the collector, later the derive job) runs
under."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ecoflow_stats.jobs import SupervisedTask, SupervisedTaskHandle
from tests.fakes import FakeClock


@pytest.mark.anyio
async def test_a_crashing_task_restarts_with_doubling_backoff() -> None:
    """Acquisition: a tick failure or an unexpected crash must not stop
    future ticks — the supervisor must restart the task, waiting longer
    after each repeated crash rather than hammering a service that is down."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FakeClock(start)
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    task = SupervisedTask(name="test", target=flaky, clock=clock)
    with pytest.raises(asyncio.CancelledError):
        await task.run()

    assert task.restart_count == 2
    assert (clock.now() - start).total_seconds() == 5 + 10


@pytest.mark.anyio
async def test_backoff_is_capped_at_five_minutes() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FakeClock(start)
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls <= 8:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    task = SupervisedTask(name="test", target=flaky, clock=clock)
    with pytest.raises(asyncio.CancelledError):
        await task.run()

    # 5+10+20+40+80+160+300+300 = 915: the backoff doubles until it would
    # exceed 300s, then holds there rather than growing further.
    assert (clock.now() - start).total_seconds() == 915


@pytest.mark.anyio
async def test_state_reflects_the_most_recent_crash() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first crash")
        raise asyncio.CancelledError

    task = SupervisedTask(name="test", target=flaky, clock=clock)
    assert task.running is False
    assert task.last_error is None

    with pytest.raises(asyncio.CancelledError):
        await task.run()

    assert task.running is False
    assert task.last_error == "first crash"
    assert task.restart_count == 1


class _PausingClock:
    """A clock whose ``sleep_until`` pauses on a controllable event instead
    of actually waiting — lets a test observe the supervisor mid-backoff,
    deterministically, without racing the scheduler or the real clock."""

    def __init__(self) -> None:
        self.paused = asyncio.Event()
        self._resume = asyncio.Event()

    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    async def sleep_until(self, when: datetime) -> None:
        self.paused.set()
        await self._resume.wait()
        self._resume.clear()
        self.paused.clear()

    def resume(self) -> None:
        self._resume.set()


@pytest.mark.anyio
async def test_handle_reports_alive_through_a_crash_and_recovery_while_not_running() -> None:
    """The health check's 503 trigger (amendment A1: "the collector task is
    dead") must be the supervisor's own asyncio.Task having ended — not
    `.running`, which is legitimately False during a normal crash backoff
    the supervisor will recover from on its own. A health check driven by
    `.running` instead would flap unhealthy on every ordinary retry."""
    clock = _PausingClock()
    calls = 0
    second_call_started = asyncio.Event()
    blocked_forever = asyncio.Event()

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        second_call_started.set()
        await blocked_forever.wait()

    supervised = SupervisedTask(name="test", target=flaky, clock=clock)
    task = asyncio.create_task(supervised.run())
    handle = SupervisedTaskHandle(supervised=supervised, task=task)

    await asyncio.wait_for(clock.paused.wait(), timeout=1)
    assert handle.running is False  # mid-backoff after the first crash
    assert handle.alive is True  # but the supervisor's own task never ended

    clock.resume()
    await asyncio.wait_for(second_call_started.wait(), timeout=1)
    assert handle.alive is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handle.alive is False
