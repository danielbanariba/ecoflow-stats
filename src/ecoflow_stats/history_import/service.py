"""Orchestrates one import run against an already-verified snapshot:
Phase 8's outage-log parsing, the samples row transform and batched
insert, and the resulting report.

The snapshot itself (copying and verifying the legacy sources) is the
caller's responsibility (`panel_samples.make_snapshot`) — this module only
ever reads from the paths it is given, never from the live legacy files.
Recomputing outages/rollups from the newly-imported data is a later work
unit (the derive job and `outages.service.derive_outages`, Phase 11): this
run only stores the raw samples and the legacy outage-log events, and
marks outages dirty from the earliest imported sample so the next derive
tick picks the new history up (history-import requirement: "Recomputed
Events Are Authoritative Where the App's Own Data Overlaps").
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ecoflow_stats.history_import.outage_log import parse_outage_log
from ecoflow_stats.history_import.panel_samples import RejectedRow, iter_legacy_rows, transform_row
from ecoflow_stats.history_import.report import ImportReport
from ecoflow_stats.storage.legacy import LegacyStore
from ecoflow_stats.storage.samples import SampleStore

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from ecoflow_stats.storage.derivations import DerivationStore


def run_import(
    *,
    snapshot_samples_db: Path | None,
    snapshot_outage_log: Path | None,
    device_id: int,
    source_tz: str,
    import_id: int,
    writer_conn: sqlite3.Connection,
    now: datetime,
    derivation_store: DerivationStore | None = None,
) -> ImportReport:
    """Import whichever half(s) of an already-verified snapshot were
    actually given for one device, returning a report of what was
    found and done.

    Either `snapshot_samples_db` or `snapshot_outage_log` may be
    `None` (CLI-01, qa-report-data-01.md: each source is optional, and
    this is the one function that actually skips reading a source
    that was never given, rather than crashing on a `None` path) --
    the caller is responsible for rejecting the case where both are
    `None`, since that is a usage error this function has no opinion
    about.

    `derivation_store` is optional only so every existing caller and test
    that predates `storage.derivations` keeps working unchanged; the real
    CLI path always supplies it now. When given, and at least one sample
    was actually read, marks the device's `outages` derivation dirty from
    the earliest imported sample's timestamp -- never from a later one,
    so a re-run that reads the same or a narrower range never hides an
    earlier pending mark.
    """
    legacy_store = LegacyStore(writer_conn)
    sample_store = SampleStore(writer_conn)

    events_imported = 0
    outage_events_inserted = 0
    outage_events_already_present = 0
    suspected_phantom = 0
    malformed_lines = 0
    if snapshot_outage_log is not None:
        parsed_log = parse_outage_log(
            snapshot_outage_log.read_text().splitlines(), source_tz=source_tz
        )
        for event in parsed_log.events:
            was_inserted = legacy_store.upsert(
                device_id=device_id,
                start_ts=event.start_ts,
                end_ts=event.end_ts,
                soc_start=event.soc_start,
                soc_end=event.soc_end,
                logged_minutes=event.logged_minutes,
                start_line=event.start_line,
                end_line=event.end_line,
                source_tz=source_tz,
                flags=event.flags,
                import_id=import_id,
            )
            if was_inserted:
                outage_events_inserted += 1
            else:
                # CLI-02 (qa-report-data-01.md): `upsert`'s own
                # UNIQUE-constraint no-op (an already-imported event,
                # re-parsed on a later run) must be reported as
                # "already present", never silently counted among
                # this run's own insertions.
                outage_events_already_present += 1
        events_imported = len(parsed_log.events)
        suspected_phantom = sum(
            1 for event in parsed_log.events if "suspected_phantom" in event.flags
        )
        malformed_lines = len(parsed_log.malformed)

    valid_rows = []
    invalid = 0
    if snapshot_samples_db is not None:
        read_conn = sqlite3.connect(snapshot_samples_db)
        read_conn.row_factory = sqlite3.Row
        try:
            for raw in iter_legacy_rows(read_conn):
                transformed = transform_row(raw, now=now)
                if isinstance(transformed, RejectedRow):
                    invalid += 1
                    continue
                valid_rows.append((transformed.ts, transformed.reading))
        finally:
            read_conn.close()

    batch = sample_store.add_batch(device_id, 2, valid_rows)
    timestamps = [ts for ts, _ in valid_rows]

    if derivation_store is not None and timestamps:
        derivation_store.mark_dirty(device_id, "outages", min(timestamps))

    return ImportReport(
        samples_read=len(valid_rows) + invalid,
        samples_inserted=batch.inserted,
        samples_skipped_existing=batch.skipped_existing,
        samples_skipped_overlap=batch.skipped_overlap,
        samples_invalid=invalid,
        outage_events_imported=events_imported,
        outage_events_inserted=outage_events_inserted,
        outage_events_already_present=outage_events_already_present,
        outage_events_suspected_phantom=suspected_phantom,
        outage_log_malformed_lines=malformed_lines,
        earliest_ts=min(timestamps, default=None),
        latest_ts=max(timestamps, default=None),
    )


__all__ = ["run_import"]
