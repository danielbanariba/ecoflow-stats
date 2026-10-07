"""Integration tests for :class:`RunLog` (the ``app_runs`` table)."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.runs import RunLog


def _log(tmp_path: Path) -> tuple[RunLog, Database]:
    db = Database(tmp_path / "ecoflow-stats.db")
    return RunLog(db.writer), db


def test_starting_a_run_persists_it(tmp_path: Path) -> None:
    log, db = _log(tmp_path)
    try:
        run_id = log.start(started_at=1_000, app_version="0.1.0")
        assert isinstance(run_id, int)
        runs = log.covering(1_000, 1_000)
        assert len(runs) == 1
        assert runs[0].started_at == 1_000
        assert runs[0].app_version == "0.1.0"
        assert runs[0].stopped_at is None
    finally:
        db.close()


def test_tick_updates_last_tick_at(tmp_path: Path) -> None:
    log, db = _log(tmp_path)
    try:
        run_id = log.start(started_at=1_000, app_version="0.1.0")
        log.tick(run_id, at=1_060)
        runs = log.covering(1_000, 1_060)
        assert runs[0].last_tick_at == 1_060
    finally:
        db.close()


def test_stop_sets_stopped_at(tmp_path: Path) -> None:
    log, db = _log(tmp_path)
    try:
        run_id = log.start(started_at=1_000, app_version="0.1.0")
        log.stop(run_id, at=1_200)
        runs = log.covering(1_000, 1_200)
        assert runs[0].stopped_at == 1_200
    finally:
        db.close()


def test_covering_excludes_a_run_that_ended_before_the_window(tmp_path: Path) -> None:
    """A gap long after the app was last known running must not be
    attributed to that old run — it is 'app_down' territory, handled
    elsewhere, not a run that 'covers' this window."""
    log, db = _log(tmp_path)
    try:
        run_id = log.start(started_at=1_000, app_version="0.1.0")
        log.stop(run_id, at=1_100)
        runs = log.covering(5_000, 5_100)
        assert runs == []
    finally:
        db.close()


def test_covering_excludes_a_run_that_starts_after_the_window(tmp_path: Path) -> None:
    log, db = _log(tmp_path)
    try:
        log.start(started_at=9_000, app_version="0.1.0")
        runs = log.covering(1_000, 1_100)
        assert runs == []
    finally:
        db.close()


def test_covering_includes_an_ongoing_run_with_no_explicit_stop(tmp_path: Path) -> None:
    """A run that crashed (no `stop` call, only the last `tick`) must still
    be judged by how recently it ticked, not treated as covering forever
    or not at all."""
    log, db = _log(tmp_path)
    try:
        run_id = log.start(started_at=1_000, app_version="0.1.0")
        log.tick(run_id, at=1_060)
        runs = log.covering(1_030, 1_060)
        assert len(runs) == 1
    finally:
        db.close()
