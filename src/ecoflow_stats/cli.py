"""Command-line entry point: ``ecoflow-stats serve|check|import|recompute|healthcheck``.

Every subcommand shares the same fail-fast prefix — load and validate the
environment before doing anything else — so a misconfigured deployment never
gets partway into collecting, importing or serving. Only ``serve``'s
config-loading path is real in this work unit: it validates configuration
and wires up redacted logging; the real server starts once ``bootstrap.py``
and ``web/app.py`` exist. The other four subcommands have no implementation
yet and raise ``NotImplementedError`` rather than silently doing nothing, so
an operator never mistakes "not implemented" for a successful run.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

from ecoflow_stats.config import ConfigError, load_settings
from ecoflow_stats.logs import configure_logging

if TYPE_CHECKING:
    from collections.abc import Sequence

_SUBCOMMANDS = ("serve", "check", "import", "recompute", "healthcheck")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecoflow-stats")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in _SUBCOMMANDS:
        subparsers.add_parser(name)
    return parser


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

    raise NotImplementedError(f"{args.command!r} is not implemented yet")


__all__ = ["main"]
