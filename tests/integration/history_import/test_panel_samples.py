"""Tests for the legacy samples.db row transform: a 1:1 mapping onto the
21 schema-v1 columns (both schemas already share column names), with
validation that rejects, rather than silently imports, a row that cannot
be trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.history_import.panel_samples import RejectedRow, TransformedRow, transform_row

_NOW = datetime(2026, 10, 5, tzinfo=UTC)


def test_a_valid_row_maps_1_to_1_with_nulls_preserved() -> None:
    raw = {
        "ts": 1_700_000_000,
        "soc": 73,
        "grid_v": 121.5,
        "grid_hz": None,
        "ac_in_w": 0.0,
        "ac_out_w": None,
        "in_w": None,
        "out_w": None,
        "solar_in_w": None,
        "batt_in_w": None,
        "batt_out_w": None,
        "batt_temp_c": None,
        "chg_ac_wh": 2026.0,
        "chg_dc_wh": None,
        "chg_solar_wh": None,
        "dsg_ac_wh": None,
        "dsg_dc_wh": None,
        "cycles": 48,
        "soh": 99.0,
        "chg_remain_min": None,
        "dsg_remain_min": None,
    }
    result = transform_row(raw, now=_NOW)
    assert isinstance(result, TransformedRow)
    assert result.ts == 1_700_000_000
    assert result.reading == Reading(
        soc=73,
        grid_v=121.5,
        ac_in_w=0.0,
        chg_ac_wh=2026.0,
        cycles=48,
        soh=99.0,
    )
    assert result.reading.grid_hz is None
    assert result.reading.ac_out_w is None


def test_a_ts_before_2020_is_rejected_as_invalid() -> None:
    raw = {"ts": int(datetime(2019, 12, 31, tzinfo=UTC).timestamp()), "soc": 50}
    result = transform_row(raw, now=_NOW)
    assert isinstance(result, RejectedRow)
    assert result.reason


def test_a_ts_more_than_a_day_in_the_future_is_rejected_as_invalid() -> None:
    raw = {"ts": int(_NOW.timestamp()) + 2 * 86_400, "soc": 50}
    result = transform_row(raw, now=_NOW)
    assert isinstance(result, RejectedRow)
    assert result.reason


def test_a_non_numeric_non_null_value_is_rejected_as_invalid() -> None:
    raw = {"ts": 1_700_000_000, "soc": 50, "grid_v": "not-a-number"}
    result = transform_row(raw, now=_NOW)
    assert isinstance(result, RejectedRow)
    assert result.reason
