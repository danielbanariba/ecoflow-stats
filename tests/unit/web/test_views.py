"""Unit tests for the pure overview-page view model (web-ui "One Device
at a Time, With a Selector" and "Empty and Stale States Are Shown
Explicitly"; amendment items 9 and 10).

Kept separate from the route: ``build_overview_view_model`` takes a
``DeviceStatus`` (already built by ``live_status.service.get_status``)
and the device records, with no FastAPI, Jinja2, or storage dependency,
so the "what does this page show" decision is provable with plain
dataclasses.
"""

from __future__ import annotations

from ecoflow_stats.devices.reading import Reading
from ecoflow_stats.live_status.status import DeviceStatus, OutageStatus
from ecoflow_stats.storage.devices import DeviceRecord
from ecoflow_stats.web.views import build_device_options, build_overview_view_model

_NO_OUTAGE = OutageStatus(ongoing=False, since=None)


def _device(device_id: int, sn: str, name: str | None = None) -> DeviceRecord:
    return DeviceRecord(
        id=device_id,
        sn=sn,
        name=name,
        product_name=None,
        adapter_id="delta_pro",
        online=None,
        online_checked_at=None,
        created_at=0,
    )


def test_two_devices_produce_two_selector_options_with_the_selected_one_marked() -> None:
    """Scenario "Multiple devices show one at a time with a selector
    offered": the selector must list every configured device, not only
    the one currently shown."""
    records = (_device(1, "BA31ZEB1SF7F0001"), _device(2, "BA31ZEB1SF7F0002"))

    options = build_device_options(records, selected_device_id=2)

    assert [opt.id for opt in options] == [1, 2]
    assert [opt.selected for opt in options] == [False, True]


def test_a_named_device_uses_its_configured_name_not_the_masked_serial() -> None:
    records = (_device(1, "BA31ZEB1SF7F0001", name="Garage"),)

    options = build_device_options(records, selected_device_id=1)

    assert options[0].label == "Garage"


def test_an_unnamed_device_falls_back_to_its_masked_serial() -> None:
    """Pass-2 target: using the full serial here would leak it onto a
    page a LAN visitor without a password can already reach."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)

    options = build_device_options(records, selected_device_id=1)

    assert options[0].label == "…0001"


def test_a_device_with_no_recorded_sample_reports_no_data_not_a_fabricated_reading() -> None:
    """Scenario "A device with no data shows an explicit empty state":
    `has_data` must come from `status.ts`, never from a zero-valued
    reading field."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=None,
        age_s=None,
        stale=False,
        grid="unknown",
        reading=None,
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(device_records=records, selected_device_id=1, status=status)

    assert view.has_data is False


def test_a_fresh_sample_reports_data_with_its_grid_state_and_charge() -> None:
    """Pass-2 target: swapping `status.grid` for a hardcoded `"present"`
    would still pass a has-data-only test; this asserts the actual
    propagated value."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=1000,
        age_s=12,
        stale=False,
        grid="absent",
        reading=Reading(soc=42),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(device_records=records, selected_device_id=1, status=status)

    assert view.has_data is True
    assert view.grid == "absent"
    assert view.soc == 42
    assert view.stale is False
    assert view.age_s == 12


def test_a_stale_sample_is_reported_stale_alongside_its_age() -> None:
    """Scenario "Stale data is marked, not shown as current"."""
    records = (_device(1, "BA31ZEB1SF7F0001"),)
    status = DeviceStatus(
        device_id=1,
        ts=1000,
        age_s=9000,
        stale=True,
        grid="present",
        reading=Reading(soc=10),
        outage=_NO_OUTAGE,
    )

    view = build_overview_view_model(device_records=records, selected_device_id=1, status=status)

    assert view.stale is True
    assert view.age_s == 9000
