"""Driven ports: every boundary the application core depends on.

Each port is a ``typing.Protocol`` so the pure core (``devices``, ``outages``,
``energy``, ``battery``, ``grid``) never imports a concrete adapter — it only
ever depends on a shape defined here. Production adapters (``acquisition``,
``storage``, ``notifications``, ``clock``) implement these shapes structurally,
with no inheritance required.

Several referenced types (``DeviceInfo``, ``Reading``, ``Message``, ...) are
defined in work units that land after this one. The ``TYPE_CHECKING``-only
imports below are forward references to those future modules: with
``from __future__ import annotations`` every annotation is deferred to a
string and never evaluated at runtime, so this module stays importable today
regardless of whether those modules exist yet. If a later phase places a type
in a different module than guessed here, that phase only needs to fix the one
import line below — nothing else in this file changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from datetime import datetime

    from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo
    from ecoflow_stats.devices.reading import Reading
    from ecoflow_stats.notifications.messages import AlertKey, Message, PendingAlert
    from ecoflow_stats.outages.model import AppRunLike, Decision, Event, Gap
    from ecoflow_stats.storage.derivations import Derivation
    from ecoflow_stats.storage.failures import FetchFailure
    from ecoflow_stats.storage.samples import StoredSample

Origin = Literal[1, 2]
"""A sample's provenance: ``1`` collected live by this app, ``2`` imported
from the legacy ecoflow-panel history."""


class Clock(Protocol):
    """Time source, abstracted so tests can control it deterministically."""

    def now(self) -> datetime:
        """Return the current, timezone-aware UTC time."""
        ...

    async def sleep_until(self, when: datetime) -> None:
        """Suspend until ``when``; return immediately if ``when`` has passed."""
        ...


class DeviceCloud(Protocol):
    """Read-only EcoFlow cloud boundary; no method can control a device."""

    async def list_devices(self) -> list[DeviceInfo]:
        """Return the configured account's devices."""
        ...

    async def fetch_quota(self, sn: str) -> Mapping[str, object]:
        """Return one device's full raw quota payload, keyed exactly as the API returns it."""
        ...


class Notifier(Protocol):
    """Outbound alert channel. Implementations raise ``NotifyError`` on failure."""

    async def send(self, message: Message) -> None:
        """Deliver one notification message."""
        ...


class SampleStore(Protocol):
    """Immutable raw-sample storage: insert-only, never updated or deleted."""

    def add(self, device_id: int, ts: int, origin: Origin, reading: Reading) -> bool:
        """Insert one sample; return ``False`` if that device/minute already exists."""
        ...

    def latest(self, device_id: int) -> StoredSample | None:
        """Return the most recently recorded sample for a device, or ``None``."""
        ...

    def between(self, device_id: int, start: int, end: int) -> Iterator[StoredSample]:
        """Yield a device's samples within ``[start, end]``, in timestamp order."""
        ...


class FailureLog(Protocol):
    """Record of collection attempts that did not produce a sample."""

    def record(self, device_id: int, failure: FetchFailure) -> None:
        """Persist one fetch failure."""
        ...

    def between(self, device_id: int, start: int, end: int) -> list[FetchFailure]:
        """Return a device's failures within ``[start, end]``, in timestamp order."""
        ...


class OutageStore(Protocol):
    """Derived outage data: replaced wholesale from a checkpoint on recompute.

    Never commits on its own: the caller (`outages.service.derive_outages`'s
    caller) owns the one ``BEGIN IMMEDIATE`` transaction a derivation run
    writes its outputs in, alongside `DerivationStore.mark_computed`
    (design D4)."""

    def replace_from(
        self,
        device_id: int,
        start_ts: int | None,
        events: list[Event],
        gaps: list[Gap],
    ) -> None:
        """Replace every event and gap at or after ``start_ts`` (or all, when ``None``)."""
        ...

    def events(self, device_id: int, start: int, end: int) -> list[Event]:
        """Return outage events overlapping ``[start, end]``."""
        ...

    def gaps(self, device_id: int, start: int, end: int) -> list[Gap]:
        """Return gaps overlapping ``[start, end]``."""
        ...


class DerivationStore(Protocol):
    """Per-device, per-named-derivation bookkeeping: algorithm version,
    parameter hash, incremental checkpoint, and dirty marker -- what lets a
    recompute resume from the right point instead of starting over every
    time (storage requirement: outage recomputation is versioned and
    re-runnable; design-data section 4.3)."""

    def get(self, device_id: int, name: str) -> Derivation | None:
        """Return the named derivation's current bookkeeping row, or
        ``None`` if it has never been computed."""
        ...

    def mark_computed(
        self,
        device_id: int,
        name: str,
        *,
        version: int,
        params_hash: str,
        checkpoint_ts: int | None,
        computed_at: int,
    ) -> None:
        """Record the result of a completed run and clear the dirty
        marker. Never commits on its own -- see `OutageStore`."""
        ...

    def mark_dirty(self, device_id: int, name: str, from_ts: int) -> None:
        """Mark a derivation dirty from ``from_ts`` onward: a new sample, an
        import, or a parameter change all call this. Moves the dirty marker
        earlier when it is already dirty from a later point, never later."""
        ...


class RunLog(Protocol):
    """Record of application process runs: lets a gap say "the app itself
    was not running" rather than guessing from silence alone."""

    def covering(self, start: int, end: int) -> list[AppRunLike]:
        """Every run whose ``[started_at, stopped_at-or-last_tick_at]``
        interval overlaps ``[start, end]``."""
        ...


class DecisionStore(Protocol):
    """Durable user decisions on a gap or a legacy event; survive recompute."""

    def add(self, decision: Decision) -> int:
        """Store a decision, superseding the previously active one for its target."""
        ...

    def active(self, device_id: int) -> list[Decision]:
        """Return every not-yet-superseded decision for a device."""
        ...


class NotificationLedger(Protocol):
    """At-least-once delivery ledger, keyed by device, event start and kind."""

    def claim(self, key: AlertKey, title: str, body: str, now: int) -> bool:
        """Claim a pending alert slot; return ``False`` if already claimed."""
        ...

    def due(self, now: int) -> list[PendingAlert]:
        """Return pending alerts ready for a (re)delivery attempt."""
        ...

    def mark_sent(self, key: AlertKey, now: int) -> None:
        """Mark an alert as delivered."""
        ...

    def mark_failed(self, key: AlertKey, error: str, now: int) -> None:
        """Record a failed delivery attempt, to be retried later."""
        ...


__all__ = [
    "Clock",
    "DecisionStore",
    "DerivationStore",
    "DeviceCloud",
    "FailureLog",
    "NotificationLedger",
    "Notifier",
    "Origin",
    "OutageStore",
    "RunLog",
    "SampleStore",
]
