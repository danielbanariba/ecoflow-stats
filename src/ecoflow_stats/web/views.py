"""Pure view-model builders for the web pages.

Kept separate from route handlers so "what does this page show" is
testable without FastAPI, Jinja2, or storage: every function here takes
already-fetched data (a `DeviceStatus`, device records) and returns
plain dataclasses the templates render directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ecoflow_stats.live_status.status import DeviceStatus
    from ecoflow_stats.storage.devices import DeviceRecord


@dataclass(frozen=True, slots=True)
class DeviceOption:
    """One `<option>` in the named device selector (amendment item 10)."""

    id: int
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class OverviewViewModel:
    """Everything `overview.html` and `partials/live.html` render."""

    devices: tuple[DeviceOption, ...]
    selected_device_id: int
    has_data: bool
    stale: bool
    age_s: int | None
    grid: str
    soc: int | None


def device_label(record: DeviceRecord) -> str:
    """The masked-serial convention already used by `cli.py`'s
    `recompute` output and `bootstrap.build`'s notification device
    labels — a configured name is shown in full, but a bare serial is
    never shown past its last 4 characters on a page a LAN visitor
    without a password can already reach."""
    return record.name if record.name else f"…{record.sn[-4:]}"


def build_device_options(
    device_records: tuple[DeviceRecord, ...], *, selected_device_id: int
) -> tuple[DeviceOption, ...]:
    return tuple(
        DeviceOption(
            id=record.id, label=device_label(record), selected=record.id == selected_device_id
        )
        for record in device_records
    )


def build_overview_view_model(
    *,
    device_records: tuple[DeviceRecord, ...],
    selected_device_id: int,
    status: DeviceStatus,
) -> OverviewViewModel:
    """web-ui "Empty and Stale States Are Shown Explicitly": `has_data`
    is false only when the device has no recorded sample at all
    (`status.ts is None`), never inferred from a zero-valued reading."""
    return OverviewViewModel(
        devices=build_device_options(device_records, selected_device_id=selected_device_id),
        selected_device_id=selected_device_id,
        has_data=status.ts is not None,
        stale=status.stale,
        age_s=status.age_s,
        grid=status.grid,
        soc=status.reading.soc if status.reading is not None else None,
    )


__all__ = [
    "DeviceOption",
    "OverviewViewModel",
    "build_device_options",
    "build_overview_view_model",
    "device_label",
]
