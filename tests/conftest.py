"""Shared fixtures for the whole test suite."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


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
