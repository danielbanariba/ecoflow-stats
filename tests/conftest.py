"""Shared fixtures for the whole test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio's pytest plugin to the asyncio backend only.

    Without this, anyio parametrizes every ``@pytest.mark.anyio`` test over
    every backend it knows about, including trio — which is not a
    dependency of this project and is not installed.
    """
    return "asyncio"


@pytest.fixture(autouse=True)
def _ignore_the_runners_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test as if the machine had no ``TZ`` set.

    `config.load_settings` falls back to the ``TZ`` environment variable
    when ``ECOFLOW_STATS_TZ`` is unset, and most fixtures build settings
    from `os.environ`. Without this, local-day boundaries in page tests
    follow the developer's own timezone, and tests that pass in CI fail
    on a machine set to, say, ``Asia/Tokyo``. A test that needs a
    timezone sets ``ECOFLOW_STATS_TZ`` (or ``TZ``) explicitly.
    """
    monkeypatch.delenv("TZ", raising=False)


@pytest.fixture(autouse=True)
def _restore_global_logging_state() -> Iterator[None]:
    """Snapshot and restore process-wide `logging` state around every test.

    `logs.configure_logging` deliberately mutates the root logger (it is a
    startup-time side effect, by design) and so does anything that calls it,
    including `cli.main(["serve"])`. Without this, one test's logging setup
    leaks into every test that runs after it, in any file, for the rest of
    the process.
    """
    root = logging.getLogger()
    original_filters = list(root.filters)
    original_level = root.level
    noisy_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        yield
    finally:
        root.filters = original_filters
        root.setLevel(original_level)
        for name, level in noisy_levels.items():
            logging.getLogger(name).setLevel(level)
