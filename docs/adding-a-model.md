# Adding a device model

EcoFlow models report different raw key names for the same measurement. A
**device adapter** maps one model's raw cloud payload onto the app's
normalized `Reading` (state of charge, grid voltage, power flows, energy
counters, and so on — see `src/ecoflow_stats/devices/reading.py` for the
full field list). This app ships one model-specific adapter today
(`DeltaProAdapter`, for the EcoFlow DELTA Pro) plus `GenericAdapter`, a
catch-all that tries the DELTA Pro's own key names case-insensitively for
any unrecognized model.

If you own a different EcoFlow model and want it mapped by its real key
names (rather than relying on whatever `GenericAdapter` happens to match),
add a model-specific adapter. This takes four steps.

## 1. Create the adapter

Add `src/ecoflow_stats/devices/models/<model>.py`, subclassing
`MappedAdapter` (from `ecoflow_stats.devices.adapter`). Override only
`adapter_id`, `FIELDS`, and `claims()`.

`FIELDS` is a `Mapping[str, FieldSpec]`, one entry per `Reading` field you
can map. For each field, `FieldSpec` takes:

| Parameter | Meaning |
|---|---|
| `keys` | Candidate raw payload keys, tried in order; the first present with a real numeric value wins. |
| `transform` | Optional `Callable[[float], float]` applied before the bounds check (e.g. convert millivolts to volts). |
| `cast` | Applied after the bounds check; defaults to `float`. Use a rounding helper for an integer field like `soc`. |
| `bounds` | `(low, high)` checked *after* `transform` — the physically plausible range in the field's final unit. A value outside this range normalizes to `None`, not a clamped or wrong number. |
| `sentinels` | Raw hardware "no data" marker values, checked *before* `transform` — e.g. `0xFFFFFFFF` for a 32-bit "unknown" counter. |

A key with no real numeric value (missing, a string, `None`, or a `bool` —
Python's `bool` is an `int` subclass and is deliberately never treated as
a number here) normalizes to `None`, never a fabricated `0`.

### Worked example: a hypothetical "RIVER 3" adapter

Suppose the RIVER 3 reports grid voltage as `inv.acInVol` (millivolts,
same as the DELTA Pro), state of charge as `bms.soc` (a different key
family than the DELTA Pro's `ems.lcdShowSoc` chain), and has no solar
input channel at all (the field simply stays unmapped and reads `None`
for this model).

```python
# src/ecoflow_stats/devices/models/river3.py
"""Model-specific adapter for the EcoFlow RIVER 3."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.devices.adapter import FieldSpec, MappedAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ecoflow_stats.devices.adapter import DeviceInfo

_WATT_BOUNDS = (0.0, 20_000.0)


def _round_int(value: float) -> int:
    return round(value)


class River3Adapter(MappedAdapter):
    """Model-specific adapter for the EcoFlow RIVER 3."""

    adapter_id = "river3"

    FIELDS: Mapping[str, FieldSpec] = {
        "soc": FieldSpec(keys=("bms.soc",), cast=_round_int, bounds=(0.0, 100.0)),
        "grid_v": FieldSpec(
            keys=("inv.acInVol",), transform=lambda v: v / 1000.0, bounds=(0.0, 300.0)
        ),
        "grid_hz": FieldSpec(keys=("inv.acInFreq",), bounds=(0.0, 70.0)),
        "ac_in_w": FieldSpec(keys=("inv.inputWatts",), bounds=_WATT_BOUNDS),
        "ac_out_w": FieldSpec(keys=("inv.outputWatts",), bounds=_WATT_BOUNDS),
        # solar_in_w deliberately omitted: this model has no solar input
        # channel, so it is never consumed and the Reading field stays None.
    }

    def claims(self, info: DeviceInfo | None, payload: Mapping[str, object]) -> bool:
        if info is not None and info.product_name and "RIVER 3" in info.product_name:
            return True
        return "bms.soc" in payload and "inv.acInVol" in payload


__all__ = ["River3Adapter"]
```

Only map fields you can actually verify against the real device's
payload — an unmapped field stays `None`, which is always honest; a
wrongly-mapped field is not.

## 2. Register it

In `src/ecoflow_stats/devices/models/__init__.py`, prepend your new
adapter to `REGISTERED`, ahead of adapters already there — the registry
checks each one's `claims()` in order, and `GenericAdapter` must stay
last as the catch-all:

```python
from ecoflow_stats.devices.models.delta_pro import DeltaProAdapter
from ecoflow_stats.devices.models.generic import GenericAdapter
from ecoflow_stats.devices.models.river3 import River3Adapter

REGISTERED: tuple[DeviceAdapter, ...] = (River3Adapter(), DeltaProAdapter(), GenericAdapter())
```

A device can also skip `claims()`-based resolution entirely by naming the
adapter explicitly in `ECOFLOW_DEVICES` (e.g.
`ECOFLOW_DEVICES=R331ZEB4SF7A0001:river3`) — see
[`docs/configuration.md`](configuration.md).

## 3. Add a synthetic fixture

Add `tests/fixtures/payloads/river3.json` — a **synthetic** payload shaped
like the real device's response, with invented values. Never commit a
real captured payload or a real device serial;
`tests/contract/test_no_real_serials.py` scans the fixtures tree and
fails the build on anything matching a real EcoFlow serial pattern.

```json
{
  "bms.soc": 72,
  "inv.acInVol": 120500,
  "inv.acInFreq": 60.0,
  "inv.inputWatts": 310.0,
  "inv.outputWatts": 0.0
}
```

## 4. Add a mapping test

```python
# tests/unit/devices/test_river3_adapter.py
"""Unit tests for the RIVER 3 adapter's key mapping."""

from __future__ import annotations

import json
from pathlib import Path

from ecoflow_stats.devices.models.river3 import River3Adapter

_PAYLOAD = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "payloads" / "river3.json").read_text()
)


def test_claims_a_payload_carrying_the_river3_key_family() -> None:
    assert River3Adapter().claims(None, _PAYLOAD) is True


def test_full_payload_maps_the_fields_this_model_reports() -> None:
    reading = River3Adapter().normalize(_PAYLOAD).reading
    assert reading.soc == 72
    assert reading.grid_v == 120.5
    assert reading.grid_hz == 60.0
    assert reading.ac_in_w == 310.0


def test_an_unreported_field_stays_none_not_zero() -> None:
    """This model has no solar input channel — solar_in_w must read
    `None`, never a fabricated 0 (Named Defect: missing read as zero)."""
    reading = River3Adapter().normalize(_PAYLOAD).reading
    assert reading.solar_in_w is None
```

Run just this new test file first to prove it is real (it must fail
before the adapter exists, and pass once it does), then the full suite:

```sh
uv run pytest tests/unit/devices/test_river3_adapter.py -q
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## Diagnosing a real device's field names

If you have the real device but don't yet know its exact key names, run:

```sh
ecoflow-stats check --serial <SN>
```

against a configured device. It prints the account's devices, which
adapter currently resolves for the target device, every normalized field
(with `NULL` fields marked), any rejected field (sentinel or
out-of-bounds, reason only — never the raw value), and every unmapped raw
key name (names only, values withheld) — useful both for discovering
which keys a new model actually sends and for confirming your new
adapter's `FIELDS` table consumes everything it should.
