"""Unit tests for ecoflow_stats.cli: the fail-fast dispatch shell.

Every subcommand shares one fail-fast prefix (load and validate
configuration before anything else); only ``serve``'s config-loading path is
real in this work unit, and the rest must fail loudly with
``NotImplementedError`` rather than silently doing nothing. Also smoke-tests
``clock.SystemClock``, which this phase introduces alongside the CLI shell.
"""

from __future__ import annotations

import os

import pytest

from ecoflow_stats.cli import main
from ecoflow_stats.clock import SystemClock

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0001",
}


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ECOFLOW_* variable starts unset, regardless of the real shell
    environment this test happens to run in, so these tests never depend on
    (or leak into) an operator's actual configuration."""
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("command", ["serve", "check"])
def test_any_subcommand_with_a_missing_required_variable_exits_2_and_names_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    env = dict(VALID_ENV)
    del env["ECOFLOW_SECRET_KEY"]
    _set_env(monkeypatch, env)

    exit_code = main([command])

    assert exit_code == 2
    assert "ECOFLOW_SECRET_KEY" in capsys.readouterr().err


def test_serve_with_valid_configuration_starts_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, VALID_ENV)

    exit_code = main(["serve"])

    assert exit_code == 0


@pytest.mark.parametrize("command", ["check", "import", "recompute", "healthcheck"])
def test_not_yet_built_subcommands_raise_until_their_phase_lands(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """These commands have no real implementation before their own work unit
    (acquisition/check.py, history_import/, outages/service.py,
    web/routes/health.py). They must fail loudly, not silently return
    success for work that never ran."""
    _set_env(monkeypatch, VALID_ENV)

    with pytest.raises(NotImplementedError):
        main([command])


def test_system_clock_now_returns_a_timezone_aware_utc_datetime() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    assert now.utcoffset().total_seconds() == 0
