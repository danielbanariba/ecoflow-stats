"""The ``daily_rollups`` table: derived, per-device-per-day statistics,
keyed by ``(device_id, day)`` (storage requirement: derived data is
separate from raw samples and recomputable; design-data section 2).

The table carries battery, energy and grid columns together in one row
(design-data's DDL), but each capability only ever owns a subset of
those columns. :meth:`RollupStore.upsert` writes only the battery-field
subset (``soc_min``, ``soc_max``, ``cycles_last``, ``soh_last``,
``batt_temp_max``); :meth:`RollupStore.upsert_energy` (visual-QA batch
fix01, fix 6) writes only the seven energy-field columns
(``chg_ac_wh``, ``chg_dc_wh``, ``chg_solar_wh``, ``dsg_ac_wh``,
``dsg_dc_wh``, ``chg_ac_est_wh``, ``energy_flags``). Each method's
``ON CONFLICT`` clause ``UPDATE``s only its own columns, so a write
from one capability for a given ``(device_id, day)`` row never clobbers
what another capability already wrote there. The grid columns (Phase
20) still have no write path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyRollup:
    """The battery-field subset of one ``daily_rollups`` row. Every field
    is ``None`` when the day's samples never reported it — never a
    fabricated ``0`` (the project's standing NULL-discipline)."""

    device_id: int
    day: str
    soc_min: int | None
    soc_max: int | None
    cycles_last: int | None
    soh_last: float | None
    batt_temp_max: float | None


def _row_to_rollup(row: sqlite3.Row) -> DailyRollup:
    return DailyRollup(
        device_id=row["device_id"],
        day=row["day"],
        soc_min=row["soc_min"],
        soc_max=row["soc_max"],
        cycles_last=row["cycles_last"],
        soh_last=row["soh_last"],
        batt_temp_max=row["batt_temp_max"],
    )


class RollupStore:
    """Synchronous, battery-field-only ``daily_rollups`` table access.

    ``upsert`` never commits on its own: like
    `storage.outages.OutageStore.replace_from` and
    `storage.derivations.DerivationStore.mark_computed`, it is always
    called from inside the one ``BEGIN IMMEDIATE`` transaction a
    derivation run writes its outputs in, and the caller commits once
    (design D4; `rollups.service.derive_rollups` is that caller).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        device_id: int,
        day: str,
        *,
        soc_min: int | None,
        soc_max: int | None,
        cycles_last: int | None,
        soh_last: float | None,
        batt_temp_max: float | None,
    ) -> None:
        """Insert or replace one day's battery fields, touching only
        those five columns -- a fresh row gets every other column's SQL
        default, and an existing row keeps whatever energy/grid fields a
        different derivation already wrote there."""
        self._conn.execute(
            "INSERT INTO daily_rollups"
            " (device_id, day, soc_min, soc_max, cycles_last, soh_last, batt_temp_max)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (device_id, day) DO UPDATE SET"
            " soc_min = excluded.soc_min,"
            " soc_max = excluded.soc_max,"
            " cycles_last = excluded.cycles_last,"
            " soh_last = excluded.soh_last,"
            " batt_temp_max = excluded.batt_temp_max",
            (device_id, day, soc_min, soc_max, cycles_last, soh_last, batt_temp_max),
        )

    def upsert_energy(
        self,
        device_id: int,
        day: str,
        *,
        chg_ac_wh: float,
        chg_dc_wh: float,
        chg_solar_wh: float,
        dsg_ac_wh: float,
        dsg_dc_wh: float,
        chg_ac_est_wh: float,
        energy_flags: int,
    ) -> None:
        """Insert or replace one day's energy fields, touching only
        those seven columns -- a sibling to `upsert` rather than an
        extension of it, so the same column-scoped-ON-CONFLICT pattern
        this table's DDL comment calls for applies here too: a fresh
        row gets every other column's SQL default, and an existing row
        keeps whatever battery/grid fields a different derivation
        already wrote there (visual-QA batch fix01, fix 6: these seven
        columns -- `chg_ac_wh`/`chg_dc_wh`/`chg_solar_wh`/`dsg_ac_wh`/
        `dsg_dc_wh`/`chg_ac_est_wh`/`energy_flags` -- had no write path
        at all before this method; `energy.service.daily_energy_for_
        samples` computed them correctly but nothing ever persisted
        them)."""
        self._conn.execute(
            "INSERT INTO daily_rollups"
            " (device_id, day, chg_ac_wh, chg_dc_wh, chg_solar_wh, dsg_ac_wh, dsg_dc_wh,"
            " chg_ac_est_wh, energy_flags)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (device_id, day) DO UPDATE SET"
            " chg_ac_wh = excluded.chg_ac_wh,"
            " chg_dc_wh = excluded.chg_dc_wh,"
            " chg_solar_wh = excluded.chg_solar_wh,"
            " dsg_ac_wh = excluded.dsg_ac_wh,"
            " dsg_dc_wh = excluded.dsg_dc_wh,"
            " chg_ac_est_wh = excluded.chg_ac_est_wh,"
            " energy_flags = excluded.energy_flags",
            (
                device_id,
                day,
                chg_ac_wh,
                chg_dc_wh,
                chg_solar_wh,
                dsg_ac_wh,
                dsg_dc_wh,
                chg_ac_est_wh,
                energy_flags,
            ),
        )

    def get(self, device_id: int, day: str) -> DailyRollup | None:
        """Return one device's rollup row for ``day``, or ``None`` if it
        has never been computed."""
        row = self._conn.execute(
            "SELECT device_id, day, soc_min, soc_max, cycles_last, soh_last, batt_temp_max"
            " FROM daily_rollups WHERE device_id = ? AND day = ?",
            (device_id, day),
        ).fetchone()
        return _row_to_rollup(row) if row is not None else None

    def between(self, device_id: int, start_day: str, end_day: str) -> list[DailyRollup]:
        """Return a device's rollup rows within ``[start_day, end_day]``
        (inclusive, ``'YYYY-MM-DD'`` strings), in day order."""
        rows = self._conn.execute(
            "SELECT device_id, day, soc_min, soc_max, cycles_last, soh_last, batt_temp_max"
            " FROM daily_rollups WHERE device_id = ? AND day BETWEEN ? AND ? ORDER BY day",
            (device_id, start_day, end_day),
        ).fetchall()
        return [_row_to_rollup(row) for row in rows]


__all__ = ["DailyRollup", "RollupStore"]
