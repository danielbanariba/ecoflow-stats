"""Contract test for amendment A2: device adapters carry only the nullable
grid voltage; the PRESENT/ABSENT/UNJUDGED judgment itself belongs to the
outages domain (it needs the previous samples' history to detect a stale
payload, which a stateless per-payload adapter cannot see).
"""

from __future__ import annotations

import dataclasses

from ecoflow_stats.devices.adapter import Normalized
from ecoflow_stats.devices.reading import Reading


def test_reading_has_no_grid_presence_field() -> None:
    """Only `grid_v` (and `grid_hz`) represent grid state on Reading -- no
    separate boolean or tri-state 'grid present' field exists anywhere."""
    field_names = {f.name for f in dataclasses.fields(Reading)}
    assert "grid_v" in field_names
    grid_related_extra = field_names & {"grid_present", "grid_ok", "grid_status", "grid_up"}
    assert grid_related_extra == set()


def test_reading_grid_v_accepts_none_and_a_real_voltage() -> None:
    """grid_v must be nullable (unjudgeable) as well as hold a real value --
    proving it is a plain optional measurement, not a derived judgment."""
    assert Reading(grid_v=None).grid_v is None
    assert Reading(grid_v=121.7).grid_v == 121.7


def test_normalized_carries_no_grid_presence_field() -> None:
    """Normalized's own fields carry no separate grid-presence judgment
    either -- only the reading, the unmapped keys, and rejection reasons."""
    field_names = {f.name for f in dataclasses.fields(Normalized)}
    assert field_names == {"reading", "unmapped_keys", "rejected"}
