"""Adapter mapping machinery: turn a raw payload into a `Reading` through a
declarative `FIELDS` table, one `FieldSpec` per field.

`num()` encodes the one rule every adapter depends on: only a real `int`
or `float` value counts, and a `bool` — an `int` subclass in Python — is
never mistaken for one. A missing key, a non-numeric value, or a `bool`
all normalize to `None`, never to a fabricated `0` (Named Defect: missing
read as zero).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ecoflow_stats.devices.reading import Reading

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One device as returned by the account's device list.

    Lives in the pure `devices` core, not in `acquisition` (the cloud
    adapter that happens to be the one place today that constructs it):
    `DeviceAdapter.claims()` below needs this shape to resolve a model,
    and the hexagonal core must never import an adapter, even for a type
    (Named defect "Core doing I/O" — `acquisition.ecoflow_client`
    re-exports this same class for backward compatibility).
    """

    sn: str
    name: str | None
    product_name: str | None
    online: bool | None


def _is_real_number(value: object) -> bool:
    """True for an ``int``/``float``, false for everything else — crucially
    including ``bool``, which is an ``int`` subclass in Python and would
    otherwise be silently read as ``1``/``0``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def num(payload: Mapping[str, object], *keys: str) -> float | None:
    """Return the first key's value that is a real number, or ``None``.

    Mirrors the reference implementation's rule exactly: a missing key, a
    non-numeric value, and a ``bool`` all yield ``None`` — never a
    fabricated ``0``, which would read back as a real measurement.
    """
    for key in keys:
        value = payload.get(key)
        if _is_real_number(value):
            return float(value)
    return None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """How to derive one `Reading` field from a raw payload.

    ``keys`` are tried in order; the first one present with a real numeric
    value wins. ``sentinels`` are checked against the *raw* value (a
    hardware "no data" marker, before any unit conversion); ``bounds`` are
    checked against the value *after* ``transform`` (the physically
    plausible range in the field's final unit).
    """

    keys: tuple[str, ...]
    transform: Callable[[float], float] | None = None
    cast: Callable[[float], object] = float
    bounds: tuple[float, float] | None = None
    sentinels: tuple[float, ...] = ()


@dataclass(frozen=True)
class Normalized:
    """The result of normalizing one raw payload."""

    reading: Reading
    unmapped_keys: frozenset[str] = field(default_factory=frozenset)
    rejected: Mapping[str, str] = field(default_factory=dict)
    """field name -> reason: ``'sentinel'`` or ``'out_of_bounds'``."""


class DeviceAdapter(Protocol):
    """A model-specific (or generic) mapping from a raw payload to a Reading."""

    adapter_id: str

    def claims(self, info: DeviceInfo | None, payload: Mapping[str, object]) -> bool:
        """Return whether this adapter recognizes the device or payload."""
        ...

    def normalize(self, payload: Mapping[str, object]) -> Normalized:
        """Map a raw payload onto a normalized reading."""
        ...


class MappedAdapter:
    """Base adapter: maps a payload through `FIELDS`, one `FieldSpec` each.

    Subclasses set `adapter_id` and `FIELDS`, and implement `claims`.
    Matching is exact-case by default; `GenericAdapter` overrides
    `_resolve` for case-insensitive matching.
    """

    adapter_id: str
    FIELDS: Mapping[str, FieldSpec]

    def _resolve(
        self, payload: Mapping[str, object], keys: tuple[str, ...]
    ) -> tuple[float | None, str | None]:
        """Return ``(value, matched_key)`` for the first candidate key
        present with a real numeric value, or ``(None, None)``."""
        for key in keys:
            value = payload.get(key)
            if _is_real_number(value):
                return float(value), key
        return None, None

    def normalize(self, payload: Mapping[str, object]) -> Normalized:
        values: dict[str, object] = {}
        rejected: dict[str, str] = {}
        consumed: set[str] = set()
        for field_name, spec in self.FIELDS.items():
            raw, matched_key = self._resolve(payload, spec.keys)
            if matched_key is not None:
                consumed.add(matched_key)
            if raw is None:
                values[field_name] = None
                continue
            if raw in spec.sentinels:
                values[field_name] = None
                rejected[field_name] = "sentinel"
                continue
            transformed = spec.transform(raw) if spec.transform else raw
            if spec.bounds is not None and not (spec.bounds[0] <= transformed <= spec.bounds[1]):
                values[field_name] = None
                rejected[field_name] = "out_of_bounds"
                continue
            values[field_name] = spec.cast(transformed)
        unmapped = frozenset(key for key in payload if key not in consumed)
        return Normalized(reading=Reading(**values), unmapped_keys=unmapped, rejected=rejected)


__all__ = ["DeviceAdapter", "DeviceInfo", "FieldSpec", "MappedAdapter", "Normalized", "num"]
