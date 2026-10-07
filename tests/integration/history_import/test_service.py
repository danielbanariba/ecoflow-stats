"""Integration tests for the history-import orchestration: outage-log
parsing (Phase 8) plus the samples transform (this work unit), run
together against one already-verified snapshot.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ecoflow_stats.devices.reading import FIELD_NAMES, Reading
from ecoflow_stats.history_import.service import run_import
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.imports import ImportRunStore
from ecoflow_stats.storage.samples import SampleStore

_NOW = datetime(2026, 10, 5, tzinfo=UTC)


def _build_snapshot_samples_db(path: Path, rows: list[tuple[int, int]]) -> None:
    """`rows`: a list of (ts, soc) pairs; every other column stays NULL.

    `ts` and `soc` are declared INTEGER, matching the real legacy schema
    exactly (verified against the reference watcher script) — SQLite's
    type affinity means a REAL-declared `ts` would read back as a float
    and be rejected by `transform_row`'s integer check.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA user_version = 1")
        other_columns = ", ".join(f"{name} REAL" for name in FIELD_NAMES if name != "soc")
        conn.execute(f"CREATE TABLE samples (ts INTEGER, soc INTEGER, {other_columns})")
        for ts, soc in rows:
            conn.execute("INSERT INTO samples (ts, soc) VALUES (?, ?)", (ts, soc))
        conn.commit()
    finally:
        conn.close()


def _env(tmp_path: Path) -> tuple[Database, int, int]:
    db = Database(tmp_path / "ecoflow-stats.db")
    device = DeviceStore(db.writer).upsert(
        sn="BA31ZEB1SF7F0001", adapter_id="delta_pro", created_at=1
    )
    import_id = ImportRunStore(db.writer).start(
        device.id, started_at=1, source_tz="America/Tegucigalpa"
    )
    return db, device.id, import_id


def test_run_import_inserts_samples_and_outage_events_and_reports_counts(tmp_path: Path) -> None:
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        _build_snapshot_samples_db(samples_db, [(1_700_000_000, 90), (1_700_000_060, 89)])
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text(
            "2024-01-10 08:15:00\tcorte\t0\t\n2024-01-10 08:16:00\tretorno\t55\t1\n"
        )

        report = run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
        )

        assert report.samples_read == 2
        assert report.samples_inserted == 2
        assert report.samples_invalid == 0
        assert report.outage_events_imported == 1
        assert report.outage_events_inserted == 1
        assert report.outage_events_already_present == 0
        assert report.outage_events_suspected_phantom == 1
        assert report.outage_log_malformed_lines == 0
        assert report.earliest_ts == 1_700_000_000
        assert report.latest_ts == 1_700_000_060

        stored = list(SampleStore(db.writer).between(device_id, 0, 2_000_000_000))
        assert [s.ts for s in stored] == [1_700_000_000, 1_700_000_060]
        assert all(s.origin == 2 for s in stored)
    finally:
        db.close()


def test_rerunning_an_unchanged_import_leaves_counts_identical(tmp_path: Path) -> None:
    """History-import idempotency: a second run against unchanged sources
    must not duplicate a sample or an outage event."""
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        _build_snapshot_samples_db(samples_db, [(1_700_000_000, 90), (1_700_000_060, 89)])
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text(
            "2024-01-10 08:15:00\tcorte\t73\t\n2024-01-10 08:16:00\tretorno\t70\t1\n"
        )

        first = run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
        )
        second_import_id = ImportRunStore(db.writer).start(
            device_id, started_at=2, source_tz="America/Tegucigalpa"
        )
        second = run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=second_import_id,
            writer_conn=db.writer,
            now=_NOW,
        )

        assert first.samples_inserted == 2
        assert second.samples_inserted == 0
        assert second.samples_skipped_overlap == 2
        # CLI-02 (qa-report-data-01.md): the second run must report its
        # one outage event as "already present", not re-claim it as a
        # fresh insertion -- before this fix, outage_events_imported
        # alone always said "1" on both runs with no way to tell them
        # apart.
        assert first.outage_events_imported == 1
        assert first.outage_events_inserted == 1
        assert first.outage_events_already_present == 0
        assert second.outage_events_imported == 1
        assert second.outage_events_inserted == 0
        assert second.outage_events_already_present == 1
        (sample_count,) = db.writer.execute("SELECT COUNT(*) FROM samples").fetchone()
        (event_count,) = db.writer.execute("SELECT COUNT(*) FROM legacy_outages").fetchone()
        assert sample_count == 2
        assert event_count == 1
    finally:
        db.close()


def test_run_import_with_only_an_outage_log_imports_legacy_events_without_touching_samples(
    tmp_path: Path,
) -> None:
    """CLI-01 (qa-report-data-01.md): `run_import` must actually skip
    reading samples when `snapshot_samples_db` is `None`, not crash on
    `None.read_text()`/`sqlite3.connect(None)` -- the service-layer
    half of making each source optional."""
    db, device_id, import_id = _env(tmp_path)
    try:
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.parent.mkdir(parents=True)
        outage_log.write_text(
            "2024-01-10 08:15:00\tcorte\t73\t\n2024-01-10 08:16:00\tretorno\t70\t1\n"
        )

        report = run_import(
            snapshot_samples_db=None,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
        )

        assert report.outage_events_imported == 1
        assert report.outage_events_inserted == 1
        assert report.samples_read == 0
        assert report.samples_inserted == 0
        assert report.earliest_ts is None
        assert report.latest_ts is None
        (sample_count,) = db.writer.execute("SELECT COUNT(*) FROM samples").fetchone()
        assert sample_count == 0
    finally:
        db.close()


def test_resuming_after_a_simulated_partial_batch_interruption_does_not_double_count(
    tmp_path: Path,
) -> None:
    """A crash partway through a large import, re-run from the start,
    must not double-count any already-committed sample."""
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        rows = [(1_700_000_000 + i * 60, 90) for i in range(5)]
        _build_snapshot_samples_db(samples_db, rows)
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text("")

        # Simulates a crash partway through a real import: 2 of the 5 rows
        # are already durably committed (exactly as add_batch's own
        # periodic commits would leave behind) before the process died.
        SampleStore(db.writer).add_batch(
            device_id, 2, [(ts, Reading(soc=soc)) for ts, soc in rows[:2]]
        )

        report = run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
        )

        assert report.samples_inserted == 3  # only the rows not already committed
        assert report.samples_skipped_overlap == 2
        (sample_count,) = db.writer.execute("SELECT COUNT(*) FROM samples").fetchone()
        assert sample_count == 5
    finally:
        db.close()


def test_run_import_marks_outages_dirty_from_the_earliest_imported_sample(
    tmp_path: Path,
) -> None:
    """A regression that forgot this call would mean a freshly imported
    history never gets recomputed into outage statistics until some
    unrelated full recompute happens to run -- silently violating
    "Recomputed Events Are Authoritative Where the App's Own Data
    Overlaps". Only given when the caller actually has one to pass: a
    dry-run caller with no real `derivations` table must not need it."""
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        _build_snapshot_samples_db(samples_db, [(1_700_000_120, 90), (1_700_000_000, 91)])
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text("")
        derivation_store = DerivationStore(db.writer)

        run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
            derivation_store=derivation_store,
        )

        derivation = derivation_store.get(device_id, "outages")
        assert derivation is not None
        assert derivation.dirty_from_ts == 1_700_000_000  # the earlier of the two rows
    finally:
        db.close()


def test_run_import_marks_rollups_dirty_from_the_earliest_imported_sample(
    tmp_path: Path,
) -> None:
    """A regression that forgot this call would mean a freshly imported
    history never gets its daily rollups computed either:
    `rollups.service.derive_rollups` only recomputes a device's
    `daily_rollups` when its "rollups" derivation is dirty, and an
    import that marks only "outages" would leave that device's imported
    history out of `daily_rollups` forever."""
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        _build_snapshot_samples_db(samples_db, [(1_700_000_120, 90), (1_700_000_000, 91)])
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text("")
        derivation_store = DerivationStore(db.writer)

        run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
            derivation_store=derivation_store,
        )

        derivation = derivation_store.get(device_id, "rollups")
        assert derivation is not None
        assert derivation.dirty_from_ts == 1_700_000_000  # the earlier of the two rows
    finally:
        db.close()


def test_run_import_without_a_derivation_store_still_imports(tmp_path: Path) -> None:
    """`derivation_store` is optional precisely so a caller that has not
    wired one up (every pre-existing caller and test) keeps working."""
    db, device_id, import_id = _env(tmp_path)
    try:
        samples_db = tmp_path / "snapshot" / "samples.db"
        samples_db.parent.mkdir(parents=True)
        _build_snapshot_samples_db(samples_db, [(1_700_000_000, 90)])
        outage_log = tmp_path / "snapshot" / "outages.log"
        outage_log.write_text("")

        report = run_import(
            snapshot_samples_db=samples_db,
            snapshot_outage_log=outage_log,
            device_id=device_id,
            source_tz="America/Tegucigalpa",
            import_id=import_id,
            writer_conn=db.writer,
            now=_NOW,
        )

        assert report.samples_inserted == 1
    finally:
        db.close()
