"""The one-shot `check` command: one `list_devices()` and one
`fetch_quota()` call against a configured device, with no database write
at all — a way to verify a new device's keys, serial and mapping before
committing to full collection.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ecoflow_stats.acquisition.ecoflow_client import CloudError
from ecoflow_stats.devices.reading import FIELD_NAMES
from ecoflow_stats.logs import mask_serial

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    from ecoflow_stats.devices.registry import AdapterRegistry
    from ecoflow_stats.ports import DeviceCloud


async def run_check(
    *,
    configured_serials: Sequence[str],
    requested_serial: str | None,
    cloud: DeviceCloud,
    registry: AdapterRegistry,
    out: TextIO = sys.stdout,
) -> int:
    """Run the one-shot check against one configured device.

    Returns the process exit code: ``0`` ok, ``2`` a configuration problem
    (no serial configured or given, or the given one is not configured),
    ``3`` an API error from the cloud client.
    """
    target_serial = requested_serial or (configured_serials[0] if configured_serials else None)
    if target_serial is None:
        print("no device serial configured, and none given with --serial", file=out)
        return 2
    if target_serial not in configured_serials:
        print(f"{target_serial!r} is not a configured device serial", file=out)
        return 2

    try:
        devices = await cloud.list_devices()
        payload = await cloud.fetch_quota(target_serial)
    except CloudError as exc:
        print(f"API error: {exc}", file=out)
        return 3

    print("Account devices:", file=out)
    for device in devices:
        print(
            f"  {mask_serial(device.sn)}  online={device.online}  "
            f"{device.name or '(unnamed)'}  ({device.product_name or 'unknown model'})",
            file=out,
        )
    print(file=out)

    info = next((d for d in devices if d.sn == target_serial), None)
    adapter = registry.resolve(explicit_adapter_id=None, info=info, payload=payload)
    normalized = adapter.normalize(payload)

    print(f"Device {mask_serial(target_serial)} resolved adapter: {adapter.adapter_id}", file=out)
    print("Normalized reading:", file=out)
    for field_name in FIELD_NAMES:
        value = getattr(normalized.reading, field_name)
        marker = " (NULL)" if value is None else ""
        print(f"  {field_name} = {value}{marker}", file=out)

    if normalized.rejected:
        print("Rejected fields (reason only):", file=out)
        for field_name, reason in normalized.rejected.items():
            print(f"  {field_name}: {reason}", file=out)

    if normalized.unmapped_keys:
        print("Unmapped keys (names only, values withheld):", file=out)
        for key in sorted(normalized.unmapped_keys):
            print(f"  {key}", file=out)

    return 0


__all__ = ["run_check"]
