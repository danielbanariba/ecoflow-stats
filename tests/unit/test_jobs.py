"""Unit tests for :class:`SupervisedTask`, the task-restart-with-backoff
skeleton every long-running job (the collector, later the derive job) runs
under."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ecoflow_stats.jobs import SupervisedTask
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
