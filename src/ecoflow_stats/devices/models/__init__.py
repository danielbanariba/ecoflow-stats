"""The model-specific adapter registry seed.

``REGISTERED`` is the ordered sequence :class:`AdapterRegistry` checks:
each model-specific adapter first, in registration order, with
``GenericAdapter`` always last as the catch-all. Seeded with only
``GenericAdapter`` for now — a model-specific adapter (the DELTA Pro)
lands in a later work unit and is prepended here, ahead of it, when it
does, so it is always checked first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.devices.models.generic import GenericAdapter

if TYPE_CHECKING:
    from ecoflow_stats.devices.adapter import DeviceAdapter

REGISTERED: tuple[DeviceAdapter, ...] = (GenericAdapter(),)

__all__ = ["REGISTERED"]
