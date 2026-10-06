"""Integration tests for `storage.rollups.RollupStore` and
`rollups.service.derive_rollups` against real SQLite.

Storage requirement: derived data is separate from raw samples and
recomputable. Battery spec: "Charge History" and "Cycle Count and
State-of-Health Trends" (the rollup-writing half; design-data section
4.6). Design-data section 4.8: a refresh recomputes from
`local_day(dirty_from_ts)` through today, or everything on a
version/params change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.rollups import service as rollups_service
from ecoflow_stats.rollups.service import derive_rollups
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.rollups import DailyGridRollup, DailyRollup, RollupStore
from ecoflow_stats.storage.samples import SampleStore

_NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _reading(**overrides: object) -> Reading:
    return Reading(**overrides)  # type: ignore[arg-type]


class _Env:
    def __init__(self, db_path: Path) -> None:
        self.db = Database(db_path)
        self.writer = self.db.writer

    def device(self, sn: str) -> int:
        return DeviceStore(self.writer).upsert(sn=sn, adapter_id="delta_pro", created_at=1).id

    def insert(self, device_id: int, samples: list[tuple[int, Reading]]) -> None:
        store = SampleStore(self.writer)
        for ts, reading in samples:
            store.add(device_id, ts, 1, reading)

    def derive(self, device_id: int, *, now: datetime = _NOW, full: bool = False) -> bool:
        self.writer.execute("BEGIN IMMEDIATE")
        try:
            ran = derive_rollups(
                device_id,
                sample_store=SampleStore(self.writer),
                rollup_store=RollupStore(self.writer),
                derivation_store=DerivationStore(self.writer),
                now=now,
                full=full,
            )
        except Exception:
            self.writer.rollback()
            raise
        self.writer.commit()
        return ran

    def rollup(self, device_id: int, day: str) -> DailyRollup | None:
        return RollupStore(self.writer).get(device_id, day)

    def grid_rollup(self, device_id: int, day: str) -> DailyGridRollup | None:
        rows = RollupStore(self.writer).grid_between(device_id, day, day)
        return rows[0] if rows else None

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    created = _Env(tmp_path / "ecoflow-stats.db")
    yield created
    created.close()


# --- RollupStore: keying and column-scoped upsert --------------------------


def test_rollup_rows_are_keyed_by_device_id_and_day(env: _Env) -> None:
    """Storage requirement "Schema Supports Multiple Devices": two
    devices rolling up the same calendar day must not collide into one
    row -- a defect dropping `device_id` from the key would silently
    merge two stations' battery stats."""
    store = RollupStore(env.writer)
    device_a = env.device("BA31ZEB1SF7F0001")
    device_b = env.device("BA31ZEB1SF7F0002")

    store.upsert(
        device_a,
        "2026-10-01",
        soc_min=40,
        soc_max=90,
        cycles_last=10,
        soh_last=98.0,
        batt_temp_max=25.0,
    )
    store.upsert(
        device_b,
        "2026-10-01",
        soc_min=10,
        soc_max=50,
        cycles_last=3,
        soh_last=99.0,
        batt_temp_max=22.0,
    )
    env.writer.commit()

    row_a = store.get(device_a, "2026-10-01")
    row_b = store.get(device_b, "2026-10-01")
    assert row_a is not None and row_a.soc_min == 40
    assert row_b is not None and row_b.soc_min == 10


def test_upsert_on_the_same_key_replaces_rather_than_duplicates(env: _Env) -> None:
    """A re-run of a day's rollup (recompute is idempotent-by-construction,
    never additive, per the same rule `OutageStore.replace_from` already
    enforces for events and gaps) must update the existing row, not
    accumulate a duplicate `(device_id, day)` entry."""
    store = RollupStore(env.writer)
    device_id = env.device("BA31ZEB1SF7F0001")
    store.upsert(
        device_id,
        "2026-10-01",
        soc_min=40,
        soc_max=90,
        cycles_last=10,
        soh_last=98.0,
        batt_temp_max=25.0,
    )
    env.writer.commit()

    store.upsert(
        device_id,
        "2026-10-01",
        soc_min=35,
        soc_max=92,
        cycles_last=11,
        soh_last=97.5,
        batt_temp_max=27.0,
    )
    env.writer.commit()

    row = store.get(device_id, "2026-10-01")
    assert row is not None
    assert (row.soc_min, row.soc_max, row.cycles_last, row.soh_last, row.batt_temp_max) == (
        35,
        92,
        11,
        97.5,
        27.0,
    )
    (count,) = env.writer.execute(
        "SELECT COUNT(*) FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert count == 1


def test_upsert_never_touches_another_capabilitys_already_written_columns(env: _Env) -> None:
    """Schema-extensibility guarantee the design calls for: a future
    energy-field write (Phase 18) to the same `(device_id, day)` row
    must survive this capability's own later battery-field upsert -- a
    defect that updated every column instead of only the five battery
    ones would silently erase it."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.writer.execute(
        "INSERT INTO daily_rollups (device_id, day, chg_ac_wh) VALUES (?, ?, ?)",
        (device_id, "2026-10-01", 1234.5),
    )
    env.writer.commit()

    RollupStore(env.writer).upsert(
        device_id,
        "2026-10-01",
        soc_min=40,
        soc_max=90,
        cycles_last=10,
        soh_last=98.0,
        batt_temp_max=25.0,
    )
    env.writer.commit()

    (chg_ac_wh,) = env.writer.execute(
        "SELECT chg_ac_wh FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert chg_ac_wh == 1234.5


def test_upsert_energy_never_touches_another_capabilitys_already_written_columns(
    env: _Env,
) -> None:
    """Visual-QA batch fix01, fix 6: mirrors `upsert`'s own column-
    scoping guarantee for the energy side -- a later battery-field
    write to the same `(device_id, day)` row must survive an earlier
    energy-field upsert, and vice versa."""
    device_id = env.device("BA31ZEB1SF7F0001")
    RollupStore(env.writer).upsert(
        device_id,
        "2026-10-01",
        soc_min=40,
        soc_max=90,
        cycles_last=10,
        soh_last=98.0,
        batt_temp_max=25.0,
    )
    env.writer.commit()

    RollupStore(env.writer).upsert_energy(
        device_id,
        "2026-10-01",
        chg_ac_wh=120.5,
        chg_dc_wh=10.0,
        chg_solar_wh=300.0,
        dsg_ac_wh=50.0,
        dsg_dc_wh=5.0,
        chg_ac_est_wh=20.0,
        energy_flags=1,
    )
    env.writer.commit()

    (soc_min,) = env.writer.execute(
        "SELECT soc_min FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert soc_min == 40
    row = env.writer.execute(
        "SELECT chg_ac_wh, chg_dc_wh, chg_solar_wh, dsg_ac_wh, dsg_dc_wh,"
        " chg_ac_est_wh, energy_flags FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert tuple(row) == (120.5, 10.0, 300.0, 50.0, 5.0, 20.0, 1)


def test_upsert_grid_never_touches_another_capabilitys_already_written_columns(env: _Env) -> None:
    """Mirrors `upsert_energy`'s own column-scoping guarantee (fix01) for
    the grid side (task 20.2): a later grid-field write to the same
    `(device_id, day)` row must survive an earlier battery-field
    upsert, and vice versa -- a defect that updated every column
    instead of only the 7 grid ones would silently erase the battery
    fields."""
    device_id = env.device("BA31ZEB1SF7F0001")
    RollupStore(env.writer).upsert(
        device_id,
        "2026-10-01",
        soc_min=40,
        soc_max=90,
        cycles_last=10,
        soh_last=98.0,
        batt_temp_max=25.0,
    )
    env.writer.commit()

    RollupStore(env.writer).upsert_grid(
        device_id,
        "2026-10-01",
        grid_v_min=118.0,
        grid_v_avg=120.0,
        grid_v_max=122.0,
        grid_hz_min=59.8,
        grid_hz_avg=60.0,
        grid_hz_max=60.2,
        grid_readings=3,
    )
    env.writer.commit()

    (soc_min,) = env.writer.execute(
        "SELECT soc_min FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert soc_min == 40
    row = env.writer.execute(
        "SELECT grid_v_min, grid_v_avg, grid_v_max, grid_hz_min, grid_hz_avg, grid_hz_max,"
        " grid_readings FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert tuple(row) == (118.0, 120.0, 122.0, 59.8, 60.0, 60.2, 3)


def test_upsert_grid_on_the_same_key_replaces_rather_than_duplicates(env: _Env) -> None:
    device_id = env.device("BA31ZEB1SF7F0001")
    store = RollupStore(env.writer)
    store.upsert_grid(
        device_id,
        "2026-10-01",
        grid_v_min=118.0,
        grid_v_avg=120.0,
        grid_v_max=122.0,
        grid_hz_min=59.8,
        grid_hz_avg=60.0,
        grid_hz_max=60.2,
        grid_readings=3,
    )
    env.writer.commit()

    store.upsert_grid(
        device_id,
        "2026-10-01",
        grid_v_min=None,
        grid_v_avg=None,
        grid_v_max=None,
        grid_hz_min=None,
        grid_hz_avg=None,
        grid_hz_max=None,
        grid_readings=0,
    )
    env.writer.commit()

    (grid_v_min, grid_readings) = env.writer.execute(
        "SELECT grid_v_min, grid_readings FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert (grid_v_min, grid_readings) == (None, 0)
    (count,) = env.writer.execute(
        "SELECT COUNT(*) FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert count == 1


def test_upsert_energy_on_the_same_key_replaces_rather_than_duplicates(env: _Env) -> None:
    device_id = env.device("BA31ZEB1SF7F0001")
    store = RollupStore(env.writer)
    store.upsert_energy(
        device_id,
        "2026-10-01",
        chg_ac_wh=100.0,
        chg_dc_wh=0.0,
        chg_solar_wh=0.0,
        dsg_ac_wh=0.0,
        dsg_dc_wh=0.0,
        chg_ac_est_wh=0.0,
        energy_flags=0,
    )
    env.writer.commit()

    store.upsert_energy(
        device_id,
        "2026-10-01",
        chg_ac_wh=250.0,
        chg_dc_wh=0.0,
        chg_solar_wh=0.0,
        dsg_ac_wh=0.0,
        dsg_dc_wh=0.0,
        chg_ac_est_wh=0.0,
        energy_flags=4,
    )
    env.writer.commit()

    (chg_ac_wh, energy_flags) = env.writer.execute(
        "SELECT chg_ac_wh, energy_flags FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert (chg_ac_wh, energy_flags) == (250.0, 4)
    (count,) = env.writer.execute(
        "SELECT COUNT(*) FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "2026-10-01"),
    ).fetchone()
    assert count == 1


# --- derive_rollups: energy-field aggregation (visual-QA batch fix01, fix 6) -


def test_a_full_recompute_also_persists_energy_fields_for_the_same_day(env: _Env) -> None:
    """Fix 5+6's core claim: one `derive_rollups` run produces both
    battery and energy columns for the same row, matching the design's
    "one row, additively extended by phase" intent. Pass-1: before fix
    6, `storage.rollups.RollupStore.upsert` had no energy-column write
    path at all, so `chg_ac_wh` etc. would stay their SQL default of
    `NULL`/`0` forever even though `energy.service.daily_energy_for_
    samples` computed the real figures correctly."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(
        device_id,
        [
            (0, _reading(soc=90, chg_ac_wh=0.0)),
            (3_600, _reading(soc=85, chg_ac_wh=60.0)),  # +60 Wh in 1h
        ],
    )

    assert env.derive(device_id) is True

    (chg_ac_wh,) = env.writer.execute(
        "SELECT chg_ac_wh FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "1970-01-01"),
    ).fetchone()
    assert chg_ac_wh == pytest.approx(60.0)


def test_a_counter_reset_is_flagged_in_the_persisted_energy_flags_bitmask(env: _Env) -> None:
    """The DDL documents `energy_flags` as a bitmask (1 counter_reset,
    2 implausible_jump, 4 gap_prorated); a defect that stored the
    flag names as a string, or dropped them, would silently lose this
    signal in storage even though `DailyEnergy.flags` reported it."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(
        device_id,
        [
            (0, _reading(soc=90, chg_ac_wh=100.0)),
            (3_600, _reading(soc=85, chg_ac_wh=10.0)),  # counter reset: 10 < 100
        ],
    )

    assert env.derive(device_id) is True

    (energy_flags,) = env.writer.execute(
        "SELECT energy_flags FROM daily_rollups WHERE device_id = ? AND day = ?",
        (device_id, "1970-01-01"),
    ).fetchone()
    assert energy_flags == 1


# --- derive_rollups: grid-field aggregation (task 20.2) --------------------


def test_a_full_recompute_also_persists_grid_fields_for_the_same_day(env: _Env) -> None:
    """Task 20.2's core claim: the same `derive_rollups` run that fills
    battery and energy columns also fills the grid columns for that
    day, from the PRESENT-judged samples only. Pass-1: before this
    change, `storage.rollups.RollupStore` had no grid-column write
    path at all, so `grid_v_min`/etc. would stay their SQL default of
    NULL forever even once `grid.quality.grid_quality_range` existed."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(
        device_id,
        [
            (0, _reading(grid_v=118.0, grid_hz=59.8)),
            (60, _reading(grid_v=122.0, grid_hz=60.2)),
        ],
    )

    assert env.derive(device_id) is True

    row = env.grid_rollup(device_id, "1970-01-01")
    assert row is not None
    assert (row.grid_v_min, row.grid_v_avg, row.grid_v_max) == (118.0, 120.0, 122.0)
    assert row.grid_readings == 2


def test_a_day_with_no_grid_present_samples_persists_null_ranges_not_zero(env: _Env) -> None:
    """Scenario "A day with no grid-present samples reports no range"
    (grid-quality spec), proven at the persistence layer: a day whose
    only samples judge grid absent must store NULL grid columns, never
    a fabricated `0.0` that a chart could mistake for "zero volts"."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(device_id, [(0, _reading(grid_v=10.0, grid_hz=59.9))])  # below the 50V floor

    assert env.derive(device_id) is True

    row = env.grid_rollup(device_id, "1970-01-01")
    assert row is not None
    assert row.grid_v_min is None
    assert row.grid_readings == 0


# --- derive_rollups: battery-field aggregation ------------------------------


def test_a_full_recompute_aggregates_each_day_independently(env: _Env) -> None:
    """Task 17.2: `cycles_last`, `soh_last`, `soc_min`/`max`,
    `batt_temp_max` per day. Also covers the NULL-discipline rule: a
    sample missing a field must not pull that day's range toward zero,
    and a field's "last" value must be the last sample that actually
    reported it, not simply the chronologically last sample in the
    day."""
    device_id = env.device("BA31ZEB1SF7F0001")
    day_one = [
        (0, _reading(soc=90, cycles=10, soh=98.0, batt_temp_c=24.0)),
        (3600, _reading(soc=70, cycles=None, soh=None, batt_temp_c=None)),
    ]
    day_two = [(86_400, _reading(soc=60, cycles=11, soh=97.5, batt_temp_c=30.0))]
    env.insert(device_id, day_one + day_two)

    assert env.derive(device_id) is True

    first = env.rollup(device_id, "1970-01-01")
    assert first is not None
    assert (
        first.soc_min,
        first.soc_max,
        first.cycles_last,
        first.soh_last,
        first.batt_temp_max,
    ) == (
        70,
        90,
        10,
        98.0,
        24.0,
    )
    second = env.rollup(device_id, "1970-01-02")
    assert second is not None
    assert (
        second.soc_min,
        second.soc_max,
        second.cycles_last,
        second.soh_last,
        second.batt_temp_max,
    ) == (60, 60, 11, 97.5, 30.0)


def test_an_incremental_recompute_only_touches_days_at_or_after_the_dirty_day(env: _Env) -> None:
    """Design-data section 4.8: "recomputes days from
    `local_day(dirty_from_ts)` ... through today". A defect that
    rescanned all history on every dirty mark would still pass a naive
    "new data shows up" check; this test instead proves an EARLIER,
    already-rolled-up day is correctly never re-queried, by giving that
    earlier day a lower `soc_min` only after the derivation already ran
    clean on it -- if the incremental run below ever re-queried day
    one, this lower value would show up as its new `soc_min`."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(device_id, [(0, _reading(soc=90))])
    assert env.derive(device_id) is True
    first_after_initial_run = env.rollup(device_id, "1970-01-01")
    assert first_after_initial_run is not None and first_after_initial_run.soc_min == 90

    env.insert(device_id, [(1_800, _reading(soc=5))])  # still day one
    env.insert(device_id, [(86_400, _reading(soc=60))])  # day two
    DerivationStore(env.writer).mark_dirty(device_id, "rollups", 86_400)

    assert env.derive(device_id) is True

    untouched_first_day = env.rollup(device_id, "1970-01-01")
    assert untouched_first_day is not None and untouched_first_day.soc_min == 90
    second_day = env.rollup(device_id, "1970-01-02")
    assert second_day is not None and second_day.soc_min == 60


def test_a_clean_derivation_does_nothing_unless_forced(env: _Env) -> None:
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(device_id, [(0, _reading(soc=90))])
    assert env.derive(device_id) is True
    computed_at_first = DerivationStore(env.writer).get(device_id, "rollups").computed_at  # type: ignore[union-attr]

    ran = env.derive(device_id, now=datetime(2026, 10, 6, 1, tzinfo=UTC))

    assert ran is False
    computed_at_after = DerivationStore(env.writer).get(device_id, "rollups").computed_at  # type: ignore[union-attr]
    assert computed_at_after == computed_at_first


def test_the_full_flag_forces_a_recompute_even_when_clean(env: _Env) -> None:
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(device_id, [(0, _reading(soc=90))])
    assert env.derive(device_id) is True
    computed_at_first = DerivationStore(env.writer).get(device_id, "rollups").computed_at  # type: ignore[union-attr]

    ran = env.derive(device_id, now=datetime(2026, 10, 6, 1, tzinfo=UTC), full=True)

    assert ran is True
    computed_at_after = DerivationStore(env.writer).get(device_id, "rollups").computed_at  # type: ignore[union-attr]
    assert computed_at_after != computed_at_first


def test_a_version_change_forces_a_full_recompute_even_without_a_dirty_mark(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design-data section 1: "a configuration change forces a full
    recompute exactly like a detector version bump does" -- the same
    rule applied to the rollup version here."""
    device_id = env.device("BA31ZEB1SF7F0001")
    env.insert(device_id, [(0, _reading(soc=90))])
    assert env.derive(device_id) is True

    monkeypatch.setattr(rollups_service, "_ROLLUP_VERSION", 2)

    ran = env.derive(device_id, now=datetime(2026, 10, 6, 1, tzinfo=UTC))

    assert ran is True
    row = DerivationStore(env.writer).get(device_id, "rollups")
    assert row is not None and row.version == 2
