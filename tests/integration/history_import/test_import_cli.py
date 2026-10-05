"""Integration tests for the `import` CLI subcommand's wiring: its two
required inputs, the configured-serial guard, and the verified snapshot
it takes before anything is read for real.

The row-by-row transform that turns the snapshot into stored samples is a
later work unit — these tests prove the snapshot half end-to-end only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ecoflow_stats.cli import main
from tests.integration.history_import.conftest import write_outage_log, write_valid_samples_db

_VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0001",
}


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_import_without_serial_fails_before_importing_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _set_env(monkeypatch, {**_VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})

    exit_code = main(["import", "--source-tz", "America/Tegucigalpa"])

    assert exit_code == 2
    assert "--serial" in capsys.readouterr().err
    assert not (tmp_path / "import-tmp").exists()


def test_import_without_source_tz_fails_before_importing_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _set_env(monkeypatch, {**_VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})

    exit_code = main(["import", "--serial", "TESTDEV0001"])

    assert exit_code == 2
    assert "--source-tz" in capsys.readouterr().err
    assert not (tmp_path / "import-tmp").exists()


def test_import_with_an_unconfigured_serial_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Guards against a typo'd serial silently importing into (or
    creating) the wrong device's history."""
    _set_env(monkeypatch, {**_VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})

    exit_code = main(["import", "--serial", "NOTCONFIGURED", "--source-tz", "America/Tegucigalpa"])

    assert exit_code == 2
    assert "NOTCONFIGURED" in capsys.readouterr().err
    assert not (tmp_path / "import-tmp").exists()


def test_import_with_valid_inputs_takes_a_verified_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    samples_db = tmp_path / "samples.db"
    outage_log = tmp_path / "outages.log"
    write_valid_samples_db(samples_db)
    write_outage_log(outage_log)
    data_dir = tmp_path / "data"
    _set_env(monkeypatch, {**_VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(data_dir)})

    exit_code = main(
        [
            "import",
            "--serial",
            "TESTDEV0001",
            "--source-tz",
            "America/Tegucigalpa",
            "--samples",
            str(samples_db),
            "--outage-log",
            str(outage_log),
        ]
    )

    assert exit_code == 0
    assert (data_dir / "import-tmp" / "samples.db").exists()
    assert (data_dir / "import-tmp" / "outages.log").exists()
