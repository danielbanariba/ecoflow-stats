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
    """Build one device's live status from real storage.

    The stale-payload window is the last `stale_repeat - 1` readings
    with a voltage before the latest one, however far back they are:
    the outage detector's own window only ever holds judged readings,
    and a reading with no voltage is never judged. `poll_interval_s`
    no longer bounds that lookback."""
    latest = sample_store.latest(device_id)
    previous = []
    if latest is not None and config.stale_repeat > 1:
        rows = sample_store.latest_with(
            device_id, "grid_v", latest.ts, limit=config.stale_repeat - 1
        )
        previous = [row.reading for row in reversed(rows)]

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
