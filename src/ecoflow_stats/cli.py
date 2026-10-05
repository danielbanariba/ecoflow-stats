"""Command-line entry point: ``ecoflow-stats serve|check|import|recompute|healthcheck``.

Every subcommand shares the same fail-fast prefix — load and validate the
environment before doing anything else — so a misconfigured deployment never
gets partway into collecting, importing or serving. ``serve``'s config-
loading path is real (the real server starts once ``bootstrap.py`` and
``web/app.py`` exist), and ``check`` runs the full one-shot device check.
The remaining three subcommands have no implementation yet and raise
``NotImplementedError`` rather than silently doing nothing, so an operator
never mistakes "not implemented" for a successful run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TYPE_CHECKING

from ecoflow_stats.acquisition.check import run_check
from ecoflow_stats.acquisition.ecoflow_client import EcoFlowCloudClient
from ecoflow_stats.config import ConfigError, load_settings
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.logs import configure_logging

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    from ecoflow_stats.config import Settings
    from ecoflow_stats.ports import DeviceCloud

_SUBCOMMANDS = ("serve", "check", "import", "recompute", "healthcheck")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecoflow-stats")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMANDS:
        subparser = subparsers.add_parser(name)
        if name == "check":
            subparser.add_argument("--serial", default=None)
    return parser


async def _run_check_command(
    settings: Settings,
    requested_serial: str | None,
    *,
    cloud: DeviceCloud | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Wire `Settings` into `run_check`'s narrower inputs.

    `cloud` is injectable so tests can exercise this wiring against a fake
    cloud instead of the real one — `main()`'s production path never
    passes it, so it always builds the real `EcoFlowCloudClient`.
    """
    client = (
        cloud
        if cloud is not None
        else EcoFlowCloudClient(settings.api_host, settings.access_key, settings.secret_key)
    )
    return await run_check(
        configured_serials=[device.serial for device in settings.devices],
        requested_serial=requested_serial,
        cloud=client,
        registry=AdapterRegistry(REGISTERED),
        out=out,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, validate configuration, then dispatch.

    Returns the process exit code for the configuration-error path (``2``)
    and for ``serve``'s current config-only behavior (``0``). Every other
    subcommand raises ``NotImplementedError`` once its own work unit has not
    yet landed.
    """
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        for error in exc.errors:
            print(error, file=sys.stderr)
        return 2

    if args.command == "serve":
        configure_logging(settings)
        return 0

    if args.command == "check":
        return asyncio.run(_run_check_command(settings, args.serial))

    raise NotImplementedError(f"{args.command!r} is not implemented yet")


__all__ = ["main"]
