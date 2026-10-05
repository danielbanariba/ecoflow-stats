"""Resolves which adapter normalizes a given device's payload.

Order: (1) an explicit ``adapter_id`` configured for this serial in
``ECOFLOW_DEVICES`` always wins; (2) otherwise, the first registered
adapter whose ``claims()`` recognizes the device or payload, checked in
registration order. ``GenericAdapter`` is always registered last and
always claims, so it is the catch-all — no device is ever rejected for
being unrecognized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo
    from ecoflow_stats.devices.adapter import DeviceAdapter


class AdapterRegistry:
    """Looks up the adapter that should normalize one device's payloads."""

    def __init__(self, registered: Sequence[DeviceAdapter]) -> None:
        self._registered = tuple(registered)
        self._by_id = {adapter.adapter_id: adapter for adapter in self._registered}

    def resolve(
        self,
        *,
        explicit_adapter_id: str | None,
        info: DeviceInfo | None,
        payload: Mapping[str, object],
    ) -> DeviceAdapter:
        """Return the adapter for this device, by explicit id or by claim.

        Raises :class:`LookupError` only if no registered adapter claims
        the payload and ``GenericAdapter`` (or an equivalent always-claims
        adapter) is not registered — a configuration error, not a normal
        outcome.
        """
        if explicit_adapter_id is not None:
            explicit = self._by_id.get(explicit_adapter_id)
            if explicit is not None:
                return explicit
        for adapter in self._registered:
            if adapter.claims(info, payload):
                return adapter
        raise LookupError(
            "no adapter claimed this payload; register a catch-all adapter (e.g. GenericAdapter)"
        )


__all__ = ["AdapterRegistry"]
