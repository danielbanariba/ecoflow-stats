"""The composition root: the one place that wires validated `Settings`
into real adapters (storage, the EcoFlow cloud client) and the pieces the
collector needs, before anything is served (design D3: `bootstrap.py` is
the only composition root).

Nothing here is pure — that is the point of keeping it in one small
module, so every other file stays either pure core or a single-purpose
adapter. `build()` only does what the collector and the health endpoint
need before the server starts: create the data directory (amendment item
4 — `config.py` stays filesystem-free), open the database, and seed one
`devices` row per configured device so the collector's first tick has a
real id to attribute samples and failures to. Starting the collector
itself happens later, inside the FastAPI lifespan (`web/app.py`), because
`asyncio.create_task` needs a running event loop that does not exist yet
when `build()` runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ecoflow_stats import __version__
from ecoflow_stats.acquisition.collector import CollectorDevice
from ecoflow_stats.acquisition.ecoflow_client import EcoFlowCloudClient
from ecoflow_stats.clock import SystemClock
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.notifications.ntfy import NtfyNotifier
from ecoflow_stats.notifications.service import NotificationService
from ecoflow_stats.outages.model import DetectorConfig
from ecoflow_stats.outages.service import build_live_outage_state
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.derivations import DerivationStore
from ecoflow_stats.storage.devices import DeviceRecord, DeviceStore
from ecoflow_stats.storage.failures import FailureLog
from ecoflow_stats.storage.notifications import NotificationLedger
from ecoflow_stats.storage.runs import RunLog
from ecoflow_stats.storage.samples import SampleStore
from ecoflow_stats.storage.state import get_or_create_secret, get_session_generation

if TYPE_CHECKING:
    from ecoflow_stats.config import Settings
    from ecoflow_stats.outages.detector import LiveOutageState
    from ecoflow_stats.ports import Clock, DeviceCloud

_DATABASE_FILENAME = "ecoflow-stats.db"
_PLACEHOLDER_ADAPTER_ID = "generic"
"""Seeded when a device has no explicit `ECOFLOW_DEVICES` adapter override.

`GenericAdapter` is always registered, so this is always a valid id. It is
a placeholder only in the `devices` table: `collect_one` resolves the real
adapter from each tick's own payload independently of this column, so
nothing downstream depends on this value being corrected later.
"""


@dataclass
class Application:
    """Everything built from `Settings` that the server and the collector
    share. Not frozen: `database` and the stores hold live connections,
    and a later phase may need to replace `run_id` across a restart.

    `secret` is the persistent session/CSRF signing key (design,
    "Session": `app_state.secret`; design D11: "changing the password
    revokes sessions; restarting the process must not"). It is read
    from (or, on first run, minted into) the `app_state` table via
    `storage.state.get_or_create_secret`, so it is stable across
    restarts of the same database, not just for the lifetime of one
    running process.

    `session_generation` is `/logout`'s own revocation counter (SEC-03,
    `web.security.revoke_all_sessions`), read from the same `app_state`
    table so a session `/logout` already revoked stays revoked across a
    restart too, instead of a freshly-reset generation of `0`
    un-revoking it the moment `secret` alone became persistent.
    """

    settings: Settings
    clock: Clock
    database: Database
    device_store: DeviceStore
    sample_store: SampleStore
    failure_log: FailureLog
    run_log: RunLog
    derivation_store: DerivationStore
    run_id: int
    cloud: DeviceCloud
    registry: AdapterRegistry
    device_records: tuple[DeviceRecord, ...]
    collector_devices: tuple[CollectorDevice, ...]
    notification_service: NotificationService | None
    live_outage_states: dict[int, LiveOutageState]
    secret: bytes
    session_generation: int


def build(
    settings: Settings,
    *,
    clock: Clock | None = None,
    cloud: DeviceCloud | None = None,
) -> Application:
    """Wire `settings` into a running `Application`.

    `clock` and `cloud` are injectable so tests can exercise this wiring
    deterministically and without any network access — the production
    path (`cli.py`'s `serve`) never passes either, so it always builds the
    real `SystemClock` and `EcoFlowCloudClient`.
    """
    active_clock = clock if clock is not None else SystemClock()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.data_dir / _DATABASE_FILENAME)

    device_store = DeviceStore(database.writer)
    sample_store = SampleStore(database.writer)
    failure_log = FailureLog(database.writer)
    run_log = RunLog(database.writer)
    derivation_store = DerivationStore(database.writer)

    now_s = int(active_clock.now().timestamp())
    device_records = tuple(
        device_store.upsert(
            sn=device.serial,
            adapter_id=device.adapter_id or _PLACEHOLDER_ADAPTER_ID,
            created_at=now_s,
        )
        for device in settings.devices
    )
    collector_devices = tuple(
        CollectorDevice(device_id=record.id, sn=record.sn, configured_adapter_id=config.adapter_id)
        for record, config in zip(device_records, settings.devices, strict=True)
    )

    active_cloud = (
        cloud
        if cloud is not None
        else EcoFlowCloudClient(settings.api_host, settings.access_key, settings.secret_key)
    )
    registry = AdapterRegistry(REGISTERED)
    run_id = run_log.start(now_s, __version__)

    # Opt-in only (notifications requirement: "Notifications Are
    # Opt-In") -- without a configured topic there is nothing for a live
    # detector replay to drive, so neither is built.
    notification_service: NotificationService | None = None
    live_outage_states: dict[int, LiveOutageState] = {}
    if settings.ntfy_topic is not None:
        device_labels = (
            {record.id: f"…{record.sn[-4:]}" for record in device_records}
            if len(device_records) > 1
            else {}
        )
        notification_service = NotificationService(
            ledger=NotificationLedger(database.writer),
            notifier=NtfyNotifier(
                base_url=settings.ntfy_url, topic=settings.ntfy_topic, token=settings.ntfy_token
            ),
            clock=active_clock,
            lang=settings.notify_lang,
            device_labels=device_labels,
        )
        detector_config = DetectorConfig(
            threshold_v=settings.outage_threshold_v, gap_threshold_s=settings.gap_threshold
        )
        live_outage_states = {
            record.id: build_live_outage_state(
                record.id,
                sample_store=sample_store,
                derivation_store=derivation_store,
                config=detector_config,
                now=active_clock.now(),
            )
            for record in device_records
        }

    return Application(
        settings=settings,
        clock=active_clock,
        database=database,
        device_store=device_store,
        sample_store=sample_store,
        failure_log=failure_log,
        run_log=run_log,
        derivation_store=derivation_store,
        run_id=run_id,
        cloud=active_cloud,
        registry=registry,
        device_records=device_records,
        collector_devices=collector_devices,
        notification_service=notification_service,
        live_outage_states=live_outage_states,
        secret=bytes.fromhex(get_or_create_secret(database.writer)),
        session_generation=get_session_generation(database.writer),
    )


__all__ = ["Application", "build"]
