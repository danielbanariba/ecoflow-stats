"""The pure half of live status: given a device's latest stored sample
(and the trailing window `judge()` needs), produce its age, staleness,
grid-presence judgment, and whether an outage is ongoing right now.

Deliberately reuses `outages.model.judge()` -- the exact same threshold
and stale-payload rule the batch detector and the live collector already
use (design D6: one detector's rules, never a second hand-rolled
comparison that could disagree with it) -- rather than deriving grid
presence from `grid_v` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ecoflow_stats.outages.model import DetectorConfig, judge

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.storage.samples import StoredSample

_DEFAULT_CONFIG = DetectorConfig()
_GRID_STATE: dict[str, Literal["present", "absent", "unknown"]] = {
    "present": "present",
    "absent": "absent",
    "unjudged": "unknown",
}


@dataclass(frozen=True, slots=True)
class OutageStatus:
    """Whether an outage is ongoing for a device right now, and when it
    started -- the live-status surface for an already-derived
    `outage_events` row, never re-detected here."""

    ongoing: bool
    since: int | None


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """One device's live status, ready to be projected into the
    versioned `/api/v1/status` JSON contract."""

    device_id: int
    ts: int | None
    age_s: int | None
    stale: bool
    grid: Literal["present", "absent", "unknown"]
    reading: Reading | None
    outage: OutageStatus


def device_status(
    device_id: int,
    *,
    latest: StoredSample | None,
    previous: Sequence[Reading],
    now: datetime,
    stale_threshold_s: int,
    ongoing_outage_since: int | None,
    config: DetectorConfig = _DEFAULT_CONFIG,
) -> DeviceStatus:
    """Build one device's status.

    `previous` must already be the caller-supplied trailing window
    `judge()` needs (the same contract `judge()` itself documents); this
    function never queries storage on its own (live_status.service owns
    that).
    """
    outage = OutageStatus(ongoing=ongoing_outage_since is not None, since=ongoing_outage_since)
    if latest is None:
        return DeviceStatus(
            device_id=device_id,
            ts=None,
            age_s=None,
            stale=False,
            grid="unknown",
            reading=None,
            outage=outage,
        )
    age_s = int(now.timestamp()) - latest.ts
    judgment = judge(latest.reading, previous, config=config)
    return DeviceStatus(
        device_id=device_id,
        ts=latest.ts,
        age_s=age_s,
        stale=age_s > stale_threshold_s,
        grid=_GRID_STATE[judgment.state],
        reading=latest.reading,
        outage=outage,
    )


__all__ = ["DeviceStatus", "OutageStatus", "device_status"]
