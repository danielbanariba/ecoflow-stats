"""The ``outage_events`` and ``gaps`` tables: derived data, replaced
wholesale from a checkpoint on every recompute (storage requirement:
derived data is separate from raw samples and recomputable; design D4).
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from ecoflow_stats.outages.model import Event, Gap

if TYPE_CHECKING:
    from collections.abc import Sequence

_EVENT_COLUMNS = (
    "device_id, start_ts, end_ts, kind, readings, start_uncertainty_s,"
    " end_uncertainty_s, start_in_gap, end_in_gap, soc_start, soc_end, soc_min,"
    " dsg_remain_min_start, legacy_id, detector_version"
)
_GAP_COLUMNS = (
    "device_id, start_ts, end_ts, cause, state_before, state_after, soc_before,"
    " soc_after, chg_ac_wh_delta, expected_in_wh, evidence, failures_json,"
    " detector_version"
)


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        start_ts=row["start_ts"],
        kind=row["kind"],
        readings=row["readings"],
        end_ts=row["end_ts"],
        start_uncertainty_s=row["start_uncertainty_s"],
        end_uncertainty_s=row["end_uncertainty_s"],
        start_in_gap=bool(row["start_in_gap"]),
        end_in_gap=bool(row["end_in_gap"]),
        soc_start=row["soc_start"],
        soc_end=row["soc_end"],
        soc_min=row["soc_min"],
        dsg_remain_min_start=row["dsg_remain_min_start"],
        legacy_id=row["legacy_id"],
        detector_version=row["detector_version"],
    )


def _row_to_gap(row: sqlite3.Row) -> Gap:
    return Gap(
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        cause=row["cause"],
        state_before=row["state_before"],
        state_after=row["state_after"],
        soc_before=row["soc_before"],
        soc_after=row["soc_after"],
        chg_ac_wh_delta=row["chg_ac_wh_delta"],
        expected_in_wh=row["expected_in_wh"],
        evidence=row["evidence"],
        failures=json.loads(row["failures_json"]),
        detector_version=row["detector_version"],
    )


class OutageStore:
    """Synchronous ``outage_events``/``gaps`` table access.

    Both tables are wholesale derived data: ``replace_from`` is the only
    write, and it always deletes before it inserts. It never commits on
    its own -- the caller wraps it, together with
    `storage.derivations.DerivationStore.mark_computed`, in the one
    ``BEGIN IMMEDIATE`` transaction a derivation run writes inside
    (design D4; `outages.service.derive_outages` is that caller).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def replace_from(
        self,
        device_id: int,
        start_ts: int | None,
        events: Sequence[Event],
        gaps: Sequence[Gap],
    ) -> None:
        """Delete every event/gap at or after ``start_ts`` (or all, when
        ``start_ts`` is ``None`` -- a full recompute), then insert the
        given ones, in the order given."""
        floor = start_ts if start_ts is not None else -(2**63)
        self._conn.execute(
            "DELETE FROM outage_events WHERE device_id = ? AND start_ts >= ?",
            (device_id, floor),
        )
        self._conn.execute(
            "DELETE FROM gaps WHERE device_id = ? AND start_ts >= ?",
            (device_id, floor),
        )
        for event in events:
            self._conn.execute(
                f"INSERT INTO outage_events ({_EVENT_COLUMNS}) VALUES ({', '.join('?' * 15)})",
                (
                    device_id,
                    event.start_ts,
                    event.end_ts,
                    event.kind,
                    event.readings,
                    event.start_uncertainty_s,
                    event.end_uncertainty_s,
                    int(event.start_in_gap),
                    int(event.end_in_gap),
                    event.soc_start,
                    event.soc_end,
                    event.soc_min,
                    event.dsg_remain_min_start,
                    event.legacy_id,
                    event.detector_version,
                ),
            )
        for gap in gaps:
            self._conn.execute(
                f"INSERT INTO gaps ({_GAP_COLUMNS}) VALUES ({', '.join('?' * 13)})",
                (
                    device_id,
                    gap.start_ts,
                    gap.end_ts,
                    gap.cause,
                    gap.state_before,
                    gap.state_after,
                    gap.soc_before,
                    gap.soc_after,
                    gap.chg_ac_wh_delta,
                    gap.expected_in_wh,
                    gap.evidence,
                    json.dumps(gap.failures),
                    gap.detector_version,
                ),
            )

    def events(self, device_id: int, start: int, end: int) -> list[Event]:
        """Events overlapping ``[start, end]``: started at or before
        ``end``, and either still open or ended at or after ``start``."""
        rows = self._conn.execute(
            "SELECT * FROM outage_events WHERE device_id = ? AND start_ts <= ?"
            " AND (end_ts IS NULL OR end_ts >= ?) ORDER BY start_ts",
            (device_id, end, start),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def gaps(self, device_id: int, start: int, end: int) -> list[Gap]:
        """Gaps overlapping ``[start, end]``, by the same rule as `events`."""
        rows = self._conn.execute(
            "SELECT * FROM gaps WHERE device_id = ? AND start_ts <= ?"
            " AND (end_ts IS NULL OR end_ts >= ?) ORDER BY start_ts",
            (device_id, end, start),
        ).fetchall()
        return [_row_to_gap(row) for row in rows]


__all__ = ["OutageStore"]
