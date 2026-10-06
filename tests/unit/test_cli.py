"""Unit tests for ecoflow_stats.cli: the fail-fast dispatch shell.

Every subcommand shares one fail-fast prefix (load and validate
configuration before anything else); only ``serve``'s config-loading path is
real in this work unit, and the rest must fail loudly with
``NotImplementedError`` rather than silently doing nothing. Also smoke-tests
``clock.SystemClock``, which this phase introduces alongside the CLI shell.
"""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats import cli
from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo
from ecoflow_stats.cli import _build_parser, _run_check_command, main
from ecoflow_stats.clock import SystemClock
from ecoflow_stats.config import load_settings
from tests.fakes import FakeDeviceCloud

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


def test_serve_with_valid_configuration_builds_the_app_and_serves_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`serve`'s stub ("config-loading path is real... once bootstrap.py
    and web/app.py exist" -- cli.py's own module docstring, written in
    Phase 1) becomes real here: it must build the composition root and
    actually hand the app to a server, not just validate configuration
    and return. The runner is monkeypatched so this never binds a real
    socket or blocks."""
    _set_env(monkeypatch, {**VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})
    calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        cli, "_uvicorn_run", lambda app, host, port: calls.append((app, host, port))
    )

    exit_code = main(["serve"])

    assert exit_code == 0
    assert len(calls) == 1
    _app, host, port = calls[0]
    assert (host, port) == ("0.0.0.0", 8080)


def test_main_dispatches_healthcheck_to_run_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, VALID_ENV)
    calls = []
    monkeypatch.setattr(cli, "run_healthcheck", lambda settings: calls.append(settings) or 0)

    exit_code = main(["healthcheck"])

    assert exit_code == 0
    assert len(calls) == 1


def test_main_dispatches_recompute_to_run_recompute_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, VALID_ENV)
    calls = []
    monkeypatch.setattr(cli, "_run_recompute_command", lambda settings: calls.append(settings) or 0)

    exit_code = main(["recompute"])

    assert exit_code == 0
    assert len(calls) == 1


def test_recompute_with_no_samples_runs_cleanly_and_reports_every_device(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full recompute against a brand-new, empty data directory must
    not require any samples to already exist -- it is also how the very
    first recompute after `serve` creates the database would run."""
    _set_env(
        monkeypatch,
        {
            **VALID_ENV,
            "ECOFLOW_DEVICES": "TESTDEV0001,TESTDEV0002",
            "ECOFLOW_STATS_DATA_DIR": str(tmp_path),
        },
    )
    settings = load_settings(os.environ)

    exit_code = cli._run_recompute_command(settings, now=datetime(2026, 10, 6, tzinfo=UTC))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count("recomputed") == 2
    assert "0 outage event(s)" in out


def test_recompute_forces_a_full_recompute_even_when_already_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Running `recompute` twice in a row must recompute both times --
    the whole point of an operator-invoked recompute is that it is never
    a silent no-op just because nothing is marked dirty."""
    _set_env(monkeypatch, {**VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})
    settings = load_settings(os.environ)

    first = cli._run_recompute_command(settings, now=datetime(2026, 10, 6, tzinfo=UTC))
    second = cli._run_recompute_command(settings, now=datetime(2026, 10, 6, 1, tzinfo=UTC))

    assert (first, second) == (0, 0)


def test_check_subparser_accepts_an_explicit_serial() -> None:
    """Pure argparse wiring -- no network, no settings, no dispatch."""
    args = _build_parser().parse_args(["check", "--serial", "SOMESERIAL01"])
    assert args.serial == "SOMESERIAL01"


def test_check_subparser_defaults_serial_to_none() -> None:
    args = _build_parser().parse_args(["check"])
    assert args.serial is None


@pytest.mark.anyio
async def test_run_check_command_extracts_configured_serials_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring extracts every configured device's serial from Settings
    and hands it, not the whole Settings object, to run_check."""
    _set_env(monkeypatch, {**VALID_ENV, "ECOFLOW_DEVICES": "TESTDEV0001,TESTDEV0002"})
    settings = load_settings(os.environ)
    cloud = FakeDeviceCloud(
        devices=[DeviceInfo(sn="TESTDEV0001", name="Garage", product_name="Mystery", online=True)],
        quota_results={"TESTDEV0001": {}},
    )
    out = io.StringIO()

    exit_code = await _run_check_command(settings, None, cloud=cloud, out=out)

    assert exit_code == 0
    assert cloud.fetch_quota_calls == ["TESTDEV0001"]


@pytest.mark.anyio
async def test_run_check_command_output_never_contains_the_configured_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard at the one layer that actually holds secrets:
    Settings carries real access/secret keys and a password here, and none
    of them may appear anywhere in the check command's captured output."""
    env = {**VALID_ENV, "ECOFLOW_STATS_PASSWORD": "a-very-secret-password"}
    _set_env(monkeypatch, env)
    settings = load_settings(os.environ)
    cloud = FakeDeviceCloud(
        devices=[DeviceInfo(sn="TESTDEV0001", name="Garage", product_name="Mystery", online=True)],
        quota_results={"TESTDEV0001": {"some.unmapped.key": "irrelevant"}},
    )
    out = io.StringIO()

    await _run_check_command(settings, None, cloud=cloud, out=out)

    text = out.getvalue()
    assert settings.access_key not in text
    assert settings.secret_key not in text
    assert settings.password not in text


def test_system_clock_now_returns_a_timezone_aware_utc_datetime() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() is not None
    assert now.utcoffset().total_seconds() == 0
