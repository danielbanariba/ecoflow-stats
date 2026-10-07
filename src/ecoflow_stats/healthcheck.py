"""The container's own healthcheck probe: a tiny synchronous HTTP client
hitting this same process's `/healthz`, so Docker's `HEALTHCHECK`
directive needs no extra tooling (no curl) in the runtime image (design
section 7).

Always probes `127.0.0.1`, never `settings.host`: the probe runs inside
the same container the server does, so loopback is reachable regardless
of what interface the server itself bound to.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from ecoflow_stats.config import Settings

_TIMEOUT_S = 5.0


def run_healthcheck(
    settings: Settings,
    *,
    get: Callable[[str], httpx.Response] | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Probe `/healthz` once and report exit 0 only on HTTP 200.

    `get` is injectable so tests never open a real socket; the default
    performs the real GET against this process's own loopback address.
    """
    getter = get if get is not None else lambda url: httpx.get(url, timeout=_TIMEOUT_S)
    url = f"http://127.0.0.1:{settings.port}/healthz"
    try:
        response = getter(url)
    except httpx.RequestError as exc:
        print(f"unreachable: {exc}", file=out)
        return 1

    if response.status_code != 200:
        print(f"unhealthy: HTTP {response.status_code}", file=out)
        return 1

    body = response.json()
    devices = body.get("devices", [])
    device_text = ", ".join(_describe_device(device) for device in devices)
    print(f"{body.get('status', 'unknown')}: {device_text or 'no devices configured'}", file=out)
    return 0


def _describe_device(device: dict[str, object]) -> str:
    age = device.get("last_sample_age_s")
    if age is None:
        return f"device #{device.get('id')} no sample yet"
    return f"device #{device.get('id')} last sample {age}s ago"


__all__ = ["run_healthcheck"]
