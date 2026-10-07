"""Unit tests for `live_status.status.device_status` (pure).

live-status requirements: "Latest Reading Per Device", "Data Age and
Staleness Indicator".
"""

from __future__ import annotations

from datetime import UTC, datetime

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.live_status.status import device_status
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.storage.samples import StoredSample

_CONFIG = DetectorConfig()
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_NOW_TS = int(_NOW.timestamp())


def _sample(ts: int, grid_v: float | None = 120.0, soc: int = 80) -> StoredSample:
    return StoredSample(device_id=1, ts=ts, origin=1, reading=Reading(grid_v=grid_v, soc=soc))


def test_the_most_recent_sample_is_returned() -> None:
    """Pass-1: without passing the latest sample's reading through,
    nothing on the live status page would ever show a real value
    (scenario "The most recent sample is returned")."""
    latest = _sample(_NOW_TS - 10, soc=77)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.reading is not None
    assert status.reading.soc == 77
    assert status.ts == _NOW_TS - 10


def test_no_data_yet_is_reported_honestly() -> None:
    """Pass-1: fabricating a zero-valued reading for a device with no
    samples would misrepresent "never collected" as "collected a zero
    reading" (scenario "No data yet is reported honestly")."""
    status = device_status(
        1, latest=None, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.reading is None
    assert status.ts is None
    assert status.age_s is None


def test_age_is_now_minus_the_samples_timestamp() -> None:
    """Pass-1: a wrong age calculation would make every staleness
    decision downstream wrong too."""
    latest = _sample(_NOW_TS - 42)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.age_s == 42


def test_a_fresh_sample_is_not_marked_stale() -> None:
    """Pass-1: marking a fresh sample stale would show a spurious
    warning on every normal page load (scenario "A fresh sample is not
    marked stale")."""
    latest = _sample(_NOW_TS - 10)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.stale is False


def test_an_old_sample_is_marked_stale() -> None:
    """Pass-1: failing to mark an old sample stale would let a silently
    dead collector look healthy forever (scenario "An old sample is
    marked stale")."""
    latest = _sample(_NOW_TS - 200)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.stale is True


def test_grid_present_above_the_threshold() -> None:
    """Pass-1: live status must reuse the same judge() threshold as the
    detector, not a separate hand-rolled comparison that could disagree
    with it."""
    latest = _sample(_NOW_TS - 10, grid_v=120.0)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.grid == "present"


def test_grid_absent_at_or_below_the_threshold() -> None:
    latest = _sample(_NOW_TS - 10, grid_v=0.0)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.grid == "absent"


def test_grid_unknown_when_voltage_is_missing() -> None:
    """Pass-1: defaulting to present or absent when grid_v is NULL would
    violate amendment A2 (grid presence must be NULL/unknown, never
    guessed, when the field is missing)."""
    latest = _sample(_NOW_TS - 10, grid_v=None)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.grid == "unknown"


def test_grid_unknown_for_a_stale_repeated_payload() -> None:
    """Pass-1: a cached cloud payload repeated 3+ times must be reported
    as unknown, not as whatever threshold it happened to read, exactly
    as the batch detector already treats it (amendment A3)."""
    repeated = Reading(grid_v=120.0, soc=80)
    latest = StoredSample(device_id=1, ts=_NOW_TS - 10, origin=1, reading=repeated)

    status = device_status(
        1,
        latest=latest,
        previous=(repeated, repeated),
        now=_NOW,
        stale_threshold_s=180,
        ongoing_outage_since=None,
    )

    assert status.grid == "unknown"


def test_an_ongoing_outage_is_reported_with_its_start_time() -> None:
    """Pass-1: without surfacing `ongoing_outage_since`, the live status
    page could not show "power has been out since HH:MM"."""
    latest = _sample(_NOW_TS - 10, grid_v=0.0)

    status = device_status(
        1,
        latest=latest,
        previous=(),
        now=_NOW,
        stale_threshold_s=180,
        ongoing_outage_since=_NOW_TS - 600,
    )

    assert status.outage.ongoing is True
    assert status.outage.since == _NOW_TS - 600


def test_no_ongoing_outage_reports_false_and_no_start_time() -> None:
    latest = _sample(_NOW_TS - 10, grid_v=120.0)

    status = device_status(
        1, latest=latest, previous=(), now=_NOW, stale_threshold_s=180, ongoing_outage_since=None
    )

    assert status.outage.ongoing is False
    assert status.outage.since is None
