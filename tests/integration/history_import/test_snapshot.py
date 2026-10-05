"""Integration tests for the verified, read-only legacy-source snapshot.

Builds a real, synthetic schema-v1 `samples.db` with sqlite3 directly (no
binary fixture, no real rows — matches the project's testing strategy for
legacy sources) rather than copying anything from Daniel's actual history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ecoflow_stats.history_import.panel_samples import SnapshotError, make_snapshot
from tests.integration.history_import.conftest import (
    V1_COLUMNS,
    write_outage_log,
    write_valid_samples_db,
)


class _SleepSpy:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def test_snapshot_copies_both_legacy_sources_into_one_directory(tmp_path: Path) -> None:
    """Amendment item 8: the import must read `outages.log` from the same
    snapshot instant as `samples.db`, not from the live, still-being-
    appended-to file."""
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    write_valid_samples_db(samples_db)
    write_outage_log(outage_log)
    snapshot_dir = tmp_path / "import-tmp"

    snapshot = make_snapshot(
        samples_db=samples_db, outage_log=outage_log, snapshot_dir=snapshot_dir
    )

    assert snapshot.samples_db.read_bytes() == samples_db.read_bytes()
    assert snapshot.outage_log.read_text() == outage_log.read_text()
    assert snapshot.samples_db.parent == snapshot_dir
    assert snapshot.outage_log.parent == snapshot_dir


def test_snapshot_preserves_a_row_still_pending_in_an_uncheckpointed_wal(tmp_path: Path) -> None:
    """A WAL database is not safely readable without its `-wal` sidecar: a
    row already committed to the WAL but not yet checkpointed into the
    main file would otherwise be silently lost from the import — a real
    risk, since the legacy watcher keeps writing during the parallel-run
    transition the import is explicitly designed to support."""
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    write_outage_log(outage_log)

    # Deliberately kept open for the whole test: closing the last
    # connection to a WAL-mode database auto-checkpoints it, which would
    # erase the exact not-yet-checkpointed state this test needs.
    conn = sqlite3.connect(samples_db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        columns_sql = ", ".join(f"{name} REAL" for name in V1_COLUMNS)
        conn.execute(f"CREATE TABLE samples ({columns_sql})")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.execute("INSERT INTO samples (ts) VALUES (12345)")
        conn.commit()
        assert samples_db.with_name("samples.db-wal").exists()

        snapshot = make_snapshot(
            samples_db=samples_db, outage_log=outage_log, snapshot_dir=tmp_path / "import-tmp"
        )

        check_conn = sqlite3.connect(snapshot.samples_db)
        try:
            rows = check_conn.execute("SELECT ts FROM samples").fetchall()
        finally:
            check_conn.close()
        assert rows == [(12345,)]
    finally:
        conn.close()


def test_the_original_sources_are_unmodified_after_snapshotting(tmp_path: Path) -> None:
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    write_valid_samples_db(samples_db)
    write_outage_log(outage_log)
    before_samples = samples_db.read_bytes()
    before_log = outage_log.read_bytes()

    make_snapshot(
        samples_db=samples_db, outage_log=outage_log, snapshot_dir=tmp_path / "import-tmp"
    )

    assert samples_db.read_bytes() == before_samples
    assert outage_log.read_bytes() == before_log


def test_a_persistently_corrupt_copy_retries_then_aborts_with_a_named_reason(
    tmp_path: Path,
) -> None:
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    samples_db.write_bytes(b"not a sqlite database" * 20)
    write_outage_log(outage_log)
    spy = _SleepSpy()

    with pytest.raises(SnapshotError, match="quick_check"):
        make_snapshot(
            samples_db=samples_db,
            outage_log=outage_log,
            snapshot_dir=tmp_path / "import-tmp",
            sleep=spy,
        )

    assert spy.waits == [2.0, 2.0, 2.0]


def test_a_copy_with_the_wrong_schema_version_is_refused(tmp_path: Path) -> None:
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    write_valid_samples_db(samples_db)
    conn = sqlite3.connect(samples_db)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    write_outage_log(outage_log)

    with pytest.raises(SnapshotError, match="schema"):
        make_snapshot(
            samples_db=samples_db, outage_log=outage_log, snapshot_dir=tmp_path / "import-tmp"
        )


def test_a_copy_missing_v1_columns_is_refused(tmp_path: Path) -> None:
    samples_db = tmp_path / "source" / "samples.db"
    outage_log = tmp_path / "source" / "outages.log"
    samples_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(samples_db)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("CREATE TABLE samples (ts INTEGER, soc INTEGER)")  # missing most v1 columns
    conn.commit()
    conn.close()
    write_outage_log(outage_log)

    with pytest.raises(SnapshotError, match="missing"):
        make_snapshot(
            samples_db=samples_db, outage_log=outage_log, snapshot_dir=tmp_path / "import-tmp"
        )
