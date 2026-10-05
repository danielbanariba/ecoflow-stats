"""Unit tests for the composition root: wiring validated Settings into
storage, seeded device rows, and the pieces the collector will need —
before the server or the collector itself ever starts (slice 10, the
"early milestone": `docker compose up --build` is the runtime proof that
everything below actually serves; these tests prove the wiring that
proof depends on).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoflow_stats import bootstrap
from ecoflow_stats.config import Settings, load_settings
from tests.fakes import FakeClock

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "test-access-key",
    "ECOFLOW_SECRET_KEY": "test-secret-key",
    # One device with no explicit adapter override, one with — proves both
    # the placeholder-seeding path and the explicit-override path.
    "ECOFLOW_DEVICES": "TESTDEV0001,TESTDEV0002:delta_pro",
}


@pytest.fixture(autouse=True)
def _clean_ecoflow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ECOFLOW_"):
            monkeypatch.delenv(name, raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Settings:
    env = {**VALID_ENV, "ECOFLOW_STATS_DATA_DIR": str(data_dir)}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings(os.environ)


def test_build_creates_the_data_directory_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Amendment item 4: the composition root creates the data directory;
    `config.py` only validates it syntactically and never touches the
    filesystem itself."""
    data_dir = tmp_path / "does-not-exist-yet"
    settings = _settings(monkeypatch, data_dir)
    assert not data_dir.exists()

    application = bootstrap.build(settings, clock=FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))

    assert data_dir.is_dir()
    application.database.close()


def test_build_is_safe_to_call_again_against_an_existing_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart must reuse an already-provisioned directory and database
    rather than failing because they already exist."""
    data_dir = tmp_path / "data"
    settings = _settings(monkeypatch, data_dir)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

    first = bootstrap.build(settings, clock=clock)
    first.database.close()
    second = bootstrap.build(settings, clock=clock)

    try:
        assert len(second.device_records) == 2
    finally:
        second.database.close()


def test_build_upserts_one_device_record_per_configured_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each configured device needs a real `devices` row before the
    collector's first tick, since `samples`/`fetch_failures` reference it
    by id. An explicit `ECOFLOW_DEVICES` adapter override is honored as
    the seed; otherwise the always-registered catch-all is used as a
    placeholder rather than leaving the column unset."""
    settings = _settings(monkeypatch, tmp_path / "data")
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

    application = bootstrap.build(settings, clock=clock)

    try:
        sns = [record.sn for record in application.device_records]
        assert sns == ["TESTDEV0001", "TESTDEV0002"]
        assert application.device_records[0].adapter_id == "generic"
        assert application.device_records[1].adapter_id == "delta_pro"
    finally:
        application.database.close()


def test_build_pairs_each_collector_device_with_its_own_device_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `CollectorDevice` whose `device_id` does not match its own real
    `devices` row would break the `samples.device_id` foreign key the
    first time the collector actually stores a sample."""
    settings = _settings(monkeypatch, tmp_path / "data")
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

    application = bootstrap.build(settings, clock=clock)

    try:
        assert len(application.collector_devices) == 2
        for record, collector_device in zip(
            application.device_records, application.collector_devices, strict=True
        ):
            assert collector_device.device_id == record.id
            assert collector_device.sn == record.sn
    finally:
        application.database.close()


def test_build_starts_one_app_run_and_returns_its_positive_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`app_runs` is how a future gap's cause can say "the app was down"
    (Phase 11) rather than silently blaming the cloud for a window the app
    itself was never running to observe."""
    settings = _settings(monkeypatch, tmp_path / "data")
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))

    application = bootstrap.build(settings, clock=clock)

    try:
        assert application.run_id > 0
        (count,) = application.database.writer.execute(
            "SELECT COUNT(*) FROM app_runs WHERE id = ?", (application.run_id,)
        ).fetchone()
        assert count == 1
    finally:
        application.database.close()
