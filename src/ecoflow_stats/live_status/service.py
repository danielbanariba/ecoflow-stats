"""Wires `live_status.status.device_status` to real storage: fetches a
device's latest sample, the trailing window `judge()` needs, and whether
an outage is ongoing right now -- all read fresh from storage on every
call, independent of any in-memory live detector state (so this works
identically whether or not notifications are configured).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.live_status.status import DeviceStatus, device_status
from ecoflow_stats.outages.model import DetectorConfig

if TYPE_CHECKING:
    from datetime import datetime

    from ecoflow_stats.ports import OutageStore, SampleStore

_DEFAULT_CONFIG = DetectorConfig()


def get_status(
    device_id: int,
    *,
    sample_store: SampleStore,
    outage_store: OutageStore,
    now: datetime,
    stale_threshold_s: int,
    poll_interval_s: int,
    config: DetectorConfig = _DEFAULT_CONFIG,
) -> DeviceStatus:
    """Build one device's live status from real storage."""
    latest = sample_store.latest(device_id)
    previous = []
    if latest is not None:
        window_start = latest.ts - poll_interval_s * config.stale_repeat
        rows = list(sample_store.between(device_id, window_start, latest.ts - 1))
        tail = rows[-(config.stale_repeat - 1) :] if config.stale_repeat > 1 else []
        previous = [row.reading for row in tail]

    now_ts = int(now.timestamp())
    ongoing_since: int | None = None
    for event in outage_store.events(device_id, now_ts, now_ts):
        if event.end_ts is None:
            ongoing_since = event.start_ts

    return device_status(
        device_id,
        latest=latest,
        previous=previous,
        now=now,
        stale_threshold_s=stale_threshold_s,
        ongoing_outage_since=ongoing_since,
        config=config,
    )


__all__ = ["get_status"]
