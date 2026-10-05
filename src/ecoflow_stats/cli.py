"""Command-line entry point: ``ecoflow-stats serve|check|import|recompute|healthcheck``.

Every subcommand shares the same fail-fast prefix — load and validate the
environment before doing anything else — so a misconfigured deployment never
gets partway into collecting, importing or serving. ``serve`` builds the
composition root (``bootstrap.py``) and the FastAPI app (``web/app.py``) and
hands it to a server; ``check`` runs the full one-shot device check;
``healthcheck`` probes this same process's own ``/healthz``. ``import`` and
``recompute`` have no implementation yet and raise ``NotImplementedError``
rather than silently doing nothing, so an operator never mistakes "not
implemented" for a successful run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TYPE_CHECKING

import uvicorn

from ecoflow_stats import bootstrap
from ecoflow_stats.acquisition.check import run_check
from ecoflow_stats.acquisition.ecoflow_client import EcoFlowCloudClient
from ecoflow_stats.config import ConfigError, load_settings
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.healthcheck import run_healthcheck
from ecoflow_stats.logs import configure_logging
from ecoflow_stats.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import TextIO

    from fastapi import FastAPI

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


def _uvicorn_run(app: FastAPI, host: str, port: int) -> None:
    """The real server runner; a thin wrapper so tests can inject a fake
    one instead of actually binding a socket and blocking forever."""
    uvicorn.run(app, host=host, port=port, log_config=None)


def _run_serve_command(
    settings: Settings,
    *,
    runner: Callable[[FastAPI, str, int], None] | None = None,
) -> int:
    """Build the composition root and serve it.

    ``runner`` is injectable for the same reason ``_run_check_command``
    injects ``cloud``: the production path (``main()``) never passes it, so
    it always calls the real, blocking ``_uvicorn_run``.
    """
    configure_logging(settings)
    application = bootstrap.build(settings)
    app = create_app(application)
    serve = runner if runner is not None else _uvicorn_run
    serve(app, settings.host, settings.port)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, validate configuration, then dispatch.

    Returns the process exit code for the configuration-error path (``2``);
    ``serve`` and ``healthcheck`` return their own real exit codes. ``import``
    and ``recompute`` raise ``NotImplementedError`` until their own work unit
    lands.
    """
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        for error in exc.errors:
            print(error, file=sys.stderr)
        return 2

    if args.command == "serve":
        return _run_serve_command(settings)

    if args.command == "check":
        return asyncio.run(_run_check_command(settings, args.serial))

    if args.command == "healthcheck":
        return run_healthcheck(settings)

    raise NotImplementedError(f"{args.command!r} is not implemented yet")


__all__ = ["main"]
