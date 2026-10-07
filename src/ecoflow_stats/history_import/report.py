"""The history-import report: what one import run found and did, printed
for the operator and stored in `import_runs.report_json` (history-import
requirement: "Import Report Summarizes the Result")."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Counts and range for one completed import run."""

    samples_read: int
    samples_inserted: int
    samples_skipped_existing: int
    samples_skipped_overlap: int
    samples_invalid: int
    outage_events_imported: int
    outage_events_inserted: int
    """How many of `outage_events_imported` were actually new rows (or
    closed a previously open event) this run -- CLI-02 (qa-report-
    data-01.md): a re-run against unchanged sources always reported
    every parsed event as freshly "imported", even though `storage.
    legacy.LegacyStore.upsert`'s own UNIQUE constraint silently made
    every one of them a no-op."""
    outage_events_already_present: int
    """The complement of `outage_events_inserted`: already-imported
    events re-parsed on this run, changing nothing (CLI-02)."""
    outage_events_suspected_phantom: int
    outage_log_malformed_lines: int
    earliest_ts: int | None
    latest_ts: int | None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    def render(self) -> str:
        """Human-readable summary printed by the `import` CLI command."""
        lines = [
            f"samples read:                            {self.samples_read}",
            f"samples inserted:                        {self.samples_inserted}",
            f"samples skipped (app's own sample wins):  {self.samples_skipped_existing}",
            f"samples skipped (already imported):       {self.samples_skipped_overlap}",
            f"samples invalid:                          {self.samples_invalid}",
            f"outage events imported:                   {self.outage_events_imported}",
            f"  of which newly inserted:                 {self.outage_events_inserted}",
            f"  of which already present:                {self.outage_events_already_present}",
            f"  of which suspected phantom:              {self.outage_events_suspected_phantom}",
            f"outage log lines skipped as malformed:    {self.outage_log_malformed_lines}",
        ]
        if self.earliest_ts is not None and self.latest_ts is not None:
            lines.append(
                f"covered range (UTC epoch s):              {self.earliest_ts} .. {self.latest_ts}"
            )
        else:
            lines.append("covered range:                            (no samples imported)")
        return "\n".join(lines)


__all__ = ["ImportReport"]
