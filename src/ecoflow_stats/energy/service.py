"""Application-layer composition for daily energy accounting.

Takes the samples a caller already queried through a `ports.SampleStore`
(this module stays pure-core-scanned like the rest of `energy/`, so it
depends only on `Reading`/`timeutil`/`accounting`, never a concrete
`storage` class) and delegates every actual computation to
`energy.accounting.daily_energy`. This is the integration point the
design's module tree describes `rollups.service.derive_rollups` as
eventually composing alongside the battery- and grid-field aggregations
("`rollups/service.py` # derive_rollups composing energy, grid, battery
daily functions").

Not yet wired into `rollups.service.derive_rollups` in this slice:
`storage.rollups.RollupStore.upsert` only accepts the 5 battery-field
columns it was built for (Phase 17) and extending it to also persist
`chg_ac_wh`/`chg_dc_wh`/`chg_solar_wh`/`dsg_ac_wh`/`dsg_dc_wh`/
`chg_ac_est_wh`/`energy_flags` requires editing `storage/rollups.py`,
which is outside this slice's declared edit surface. A future slice
whose edit surface includes `storage/rollups.py` should add that
column-scoped upsert method and have `derive_rollups` call
`daily_energy_for_samples` per day, exactly like
`rollups.service._daily_battery_fields` already does for the battery
columns. Until then, this function is ready for Phase 19's energy page
and API routes to call directly against raw samples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.energy.accounting import DailyEnergy, daily_energy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ecoflow_stats.devices.reading import Reading


def daily_energy_for_samples(
    samples: Sequence[tuple[int, Reading]],
    *,
    tz: str,
    tariff: float | None,
    currency: str,
) -> dict[str, DailyEnergy]:
    """Daily energy by source and direction for an already-queried batch
    of a device's samples -- the thin seam between a port-supplied
    sample sequence and the pure accounting engine, so a caller never
    has to import `energy.accounting` directly."""
    return daily_energy(samples, tz=tz, tariff=tariff, currency=currency)


__all__ = ["daily_energy_for_samples"]
