"""Integration tests for the `import` CLI subcommand's wiring: its two
required inputs, the configured-serial guard, the verified snapshot it
takes before anything is read for real, and the full import (or, with
`--dry-run`, a throwaway one) that follows.
"""

from __future__ import annotations

import os
import sqlite3
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


def test_import_without_any_source_fails_before_importing_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """CLI-01 (qa-report-data-01.md): with no more container-path
    defaults for `--samples`/`--outage-log`, giving neither must fail
    clearly -- not silently import nothing, and not crash deeper in
    `make_snapshot` on a `None` path."""
    _set_env(monkeypatch, {**_VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(tmp_path)})

    exit_code = main(["import", "--serial", "TESTDEV0001", "--source-tz", "America/Tegucigalpa"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--samples" in err
    assert "--outage-log" in err
    assert not (tmp_path / "import-tmp").exists()


def test_import_with_only_an_outage_log_succeeds_without_a_samples_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """CLI-01: the actually-reported scenario -- running `import` with
    only `--outage-log` (no `--samples` at all) outside a container
    used to crash on the hardcoded `/import/samples.db` default; now
    it imports just the outage log and succeeds."""
    outage_log = tmp_path / "outages.log"
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
            "--outage-log",
            str(outage_log),
        ]
    )

    assert exit_code == 0
    assert not (data_dir / "import-tmp" / "samples.db").exists()
    assert (data_dir / "import-tmp" / "outages.log").exists()
    conn = sqlite3.connect(data_dir / "ecoflow-stats.db")
    try:
        (event_count,) = conn.execute("SELECT COUNT(*) FROM legacy_outages").fetchone()
    finally:
        conn.close()
    assert event_count == 1


def test_a_given_but_missing_samples_path_fails_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """CLI-01's central Pass-1/Pass-2 proof, through the real CLI entry
    point: before this fix, `--samples <path that does not exist>`
    crashed the whole process with an uncaught `FileNotFoundError` and
    a raw traceback (exactly what used to happen outside a container
    with no flags given at all, since the old default pointed at a
    path that was never going to exist either). Now it exits 2 with a
    clear message instead of a traceback."""
    outage_log = tmp_path / "outages.log"
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
            str(tmp_path / "does-not-exist.db"),
            "--outage-log",
            str(outage_log),
        ]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not found" in err
    assert not (data_dir / "ecoflow-stats.db").exists()


def test_import_with_valid_inputs_imports_into_the_application_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    out = capsys.readouterr().out
    assert "samples inserted" in out
    assert "outage events imported" in out
    conn = sqlite3.connect(data_dir / "ecoflow-stats.db")
    try:
        (sample_count,) = conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        (event_count,) = conn.execute("SELECT COUNT(*) FROM legacy_outages").fetchone()
    finally:
        conn.close()
    assert sample_count == 0  # write_valid_samples_db seeds no rows, just the shape
    assert event_count == 1


def test_dry_run_prints_a_report_without_writing_the_application_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """An operator must be able to preview an import's report before
    trusting it with the real application database."""
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
            "--dry-run",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "outage events imported" in out
    assert "dry run" in out
    assert not (data_dir / "ecoflow-stats.db").exists()
