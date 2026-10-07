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
``dsg_dc_wh``, ``chg_ac_est_wh``, ``energy_flags``); :meth:`RollupStore.
upsert_grid` (task 20.2) writes only the seven grid-field columns
(``grid_v_min``, ``grid_v_avg``, ``grid_v_max``, ``grid_hz_min``,
``grid_hz_avg``, ``grid_hz_max``, ``grid_readings``). Each method's
``ON CONFLICT`` clause ``UPDATE``s only its own columns, so a write
from one capability for a given ``(device_id, day)`` row never clobbers
what another capability already wrote there.

Reads mirror this same column-scoping: :meth:`RollupStore.get`/
:meth:`between` return only the battery-field subset (``DailyRollup``);
:meth:`grid_between` (task 20.2's reader half) and :meth:`energy_between`
(task 19.1's reader half) each return their own capability-scoped
dataclass, rather than one dataclass carrying every column a caller
may not need.
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


@dataclass(frozen=True, slots=True)
class DailyEnergyRollup:
    """The energy-field subset of one ``daily_rollups`` row (task
    19.1's reader half). The 5 Wh fields are ``None`` only when
    `upsert_energy` never ran for that day at all (a day with no
    counter activity); ``chg_ac_est_wh``/``energy_flags`` are always
    present (the columns' own ``NOT NULL DEFAULT 0``)."""

    device_id: int
    day: str
    chg_ac_wh: float | None
    chg_dc_wh: float | None
    chg_solar_wh: float | None
    dsg_ac_wh: float | None
    dsg_dc_wh: float | None
    chg_ac_est_wh: float
    energy_flags: int


def _row_to_energy_rollup(row: sqlite3.Row) -> DailyEnergyRollup:
    return DailyEnergyRollup(
        device_id=row["device_id"],
        day=row["day"],
        chg_ac_wh=row["chg_ac_wh"],
        chg_dc_wh=row["chg_dc_wh"],
        chg_solar_wh=row["chg_solar_wh"],
        dsg_ac_wh=row["dsg_ac_wh"],
        dsg_dc_wh=row["dsg_dc_wh"],
        chg_ac_est_wh=row["chg_ac_est_wh"],
        energy_flags=row["energy_flags"],
    )


@dataclass(frozen=True, slots=True)
class DailyGridRollup:
    """The grid-field subset of one ``daily_rollups`` row (task 20.2).
    The 6 range fields are ``None`` together whenever the day had no
    grid-present judged reading at all -- never a fabricated ``0.0``
    (grid-quality requirement "Daily Voltage and Frequency Ranges",
    scenario "A day with no grid-present samples reports no range");
    ``grid_readings`` is ``0`` in that same case (the column's own
    ``NOT NULL DEFAULT 0``, never ``None``)."""

    device_id: int
    day: str
    grid_v_min: float | None
    grid_v_avg: float | None
    grid_v_max: float | None
    grid_hz_min: float | None
    grid_hz_avg: float | None
    grid_hz_max: float | None
    grid_readings: int


def _row_to_grid_rollup(row: sqlite3.Row) -> DailyGridRollup:
    return DailyGridRollup(
        device_id=row["device_id"],
        day=row["day"],
        grid_v_min=row["grid_v_min"],
        grid_v_avg=row["grid_v_avg"],
        grid_v_max=row["grid_v_max"],
        grid_hz_min=row["grid_hz_min"],
        grid_hz_avg=row["grid_hz_avg"],
        grid_hz_max=row["grid_hz_max"],
        grid_readings=row["grid_readings"],
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

    def upsert_grid(
        self,
        device_id: int,
        day: str,
        *,
        grid_v_min: float | None,
        grid_v_avg: float | None,
        grid_v_max: float | None,
        grid_hz_min: float | None,
        grid_hz_avg: float | None,
        grid_hz_max: float | None,
        grid_readings: int,
    ) -> None:
        """Insert or replace one day's grid fields, touching only those
        seven columns -- another sibling to `upsert`/`upsert_energy`,
        the same column-scoped-ON-CONFLICT pattern (task 20.2). Called
        unconditionally for every day `derive_rollups` re-aggregates
        (design D4: "each affected day is replaced wholesale"), so a
        day that had grid-present samples on a prior run but none on
        this one correctly reverts to NULL ranges / zero readings,
        rather than keeping a stale range forever."""
        self._conn.execute(
            "INSERT INTO daily_rollups"
            " (device_id, day, grid_v_min, grid_v_avg, grid_v_max,"
            " grid_hz_min, grid_hz_avg, grid_hz_max, grid_readings)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (device_id, day) DO UPDATE SET"
            " grid_v_min = excluded.grid_v_min,"
            " grid_v_avg = excluded.grid_v_avg,"
            " grid_v_max = excluded.grid_v_max,"
            " grid_hz_min = excluded.grid_hz_min,"
            " grid_hz_avg = excluded.grid_hz_avg,"
            " grid_hz_max = excluded.grid_hz_max,"
            " grid_readings = excluded.grid_readings",
            (
                device_id,
                day,
                grid_v_min,
                grid_v_avg,
                grid_v_max,
                grid_hz_min,
                grid_hz_avg,
                grid_hz_max,
                grid_readings,
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

    def grid_between(self, device_id: int, start_day: str, end_day: str) -> list[DailyGridRollup]:
        """Return a device's grid-field rollup rows within
        ``[start_day, end_day]`` (inclusive, ``'YYYY-MM-DD'`` strings),
        in day order -- the grid-field-scoped sibling of `between`
        (`web.routes.api`'s `grid/daily` route, task 20.3's API half)."""
        rows = self._conn.execute(
            "SELECT device_id, day, grid_v_min, grid_v_avg, grid_v_max,"
            " grid_hz_min, grid_hz_avg, grid_hz_max, grid_readings"
            " FROM daily_rollups WHERE device_id = ? AND day BETWEEN ? AND ? ORDER BY day",
            (device_id, start_day, end_day),
        ).fetchall()
        return [_row_to_grid_rollup(row) for row in rows]

    def energy_between(
        self, device_id: int, start_day: str, end_day: str
    ) -> list[DailyEnergyRollup]:
        """Return a device's energy-field rollup rows within
        ``[start_day, end_day]`` (inclusive, ``'YYYY-MM-DD'`` strings),
        in day order -- the energy-field-scoped sibling of `between`
        (`web.routes.api`'s `energy/daily` route, task 19.1's API
        half)."""
        rows = self._conn.execute(
            "SELECT device_id, day, chg_ac_wh, chg_dc_wh, chg_solar_wh, dsg_ac_wh, dsg_dc_wh,"
            " chg_ac_est_wh, energy_flags"
            " FROM daily_rollups WHERE device_id = ? AND day BETWEEN ? AND ? ORDER BY day",
            (device_id, start_day, end_day),
        ).fetchall()
        return [_row_to_energy_rollup(row) for row in rows]


__all__ = ["DailyEnergyRollup", "DailyGridRollup", "DailyRollup", "RollupStore"]
