"""The model-specific adapter registry seed.

``REGISTERED`` is the ordered sequence :class:`AdapterRegistry` checks:
each model-specific adapter first, in registration order, with
``GenericAdapter`` always last as the catch-all. A new model-specific
adapter is prepended ahead of the ones already here, so it is always
checked before them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.devices.models.delta_pro import DeltaProAdapter
from ecoflow_stats.devices.models.generic import GenericAdapter

if TYPE_CHECKING:
    from ecoflow_stats.devices.adapter import DeviceAdapter

REGISTERED: tuple[DeviceAdapter, ...] = (DeltaProAdapter(), GenericAdapter())

__all__ = ["REGISTERED"]
