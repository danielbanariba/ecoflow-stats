"""Command-line entry point: ``ecoflow-stats serve|check|import|recompute|healthcheck``.

Every subcommand shares the same fail-fast prefix — load and validate the
environment before doing anything else — so a misconfigured deployment never
gets partway into collecting, importing or serving. ``serve`` builds the
composition root (``bootstrap.py``) and the FastAPI app (``web/app.py``) and
hands it to a server; ``check`` runs the full one-shot device check;
``import`` takes a verified, read-only snapshot of the legacy ecoflow-panel
sources, then imports the outage log and samples into the application
database (or, with ``--dry-run``, into a throwaway one) and prints a
report; ``healthcheck`` probes this same process's own ``/healthz``.
``recompute`` has no implementation yet and raises ``NotImplementedError``
rather than silently doing nothing, so an operator never mistakes "not
implemented" for a successful run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from ecoflow_stats import bootstrap
from ecoflow_stats.acquisition.check import run_check
from ecoflow_stats.acquisition.ecoflow_client import EcoFlowCloudClient
from ecoflow_stats.config import ConfigError, load_settings
from ecoflow_stats.devices.models import REGISTERED
from ecoflow_stats.devices.registry import AdapterRegistry
from ecoflow_stats.healthcheck import run_healthcheck
from ecoflow_stats.history_import.panel_samples import SnapshotError, make_snapshot
from ecoflow_stats.history_import.report import ImportReport
from ecoflow_stats.history_import.service import run_import
from ecoflow_stats.logs import configure_logging
from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.devices import DeviceStore
from ecoflow_stats.storage.imports import ImportRunStore
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
        if name == "import":
            subparser.add_argument("--serial", default=None)
            subparser.add_argument("--source-tz", default=None)
            subparser.add_argument("--samples", default="/import/samples.db")
            subparser.add_argument("--outage-log", default="/import/outages.log")
            subparser.add_argument("--dry-run", action="store_true")
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


def _import_into(
    *,
    db_path: Path,
    serial: str,
    adapter_id: str,
    source_tz: str,
    snapshot_samples_db: Path,
    snapshot_outage_log: Path,
    now: datetime,
    record_run: bool,
) -> ImportReport:
    """Open the database at `db_path`, seed the device row, and run the
    import against it. `record_run` is false for `--dry-run`: the import
    still runs for real against `db_path` (so the report reflects real
    validation), but no `import_runs` completion is recorded — the
    caller always points `db_path` at a throwaway database for a dry run,
    never at the real application database.
    """
    database = Database(db_path)
    try:
        now_s = int(now.timestamp())
        device = DeviceStore(database.writer).upsert(
            sn=serial, adapter_id=adapter_id, created_at=now_s
        )
        import_id = ImportRunStore(database.writer).start(
            device.id, started_at=now_s, source_tz=source_tz
        )
        report = run_import(
            snapshot_samples_db=snapshot_samples_db,
            snapshot_outage_log=snapshot_outage_log,
            device_id=device.id,
            source_tz=source_tz,
            import_id=import_id,
            writer_conn=database.writer,
            now=now,
        )
        if record_run:
            ImportRunStore(database.writer).finish(
                import_id, int(now.timestamp()), report.to_json()
            )
        return report
    finally:
        database.close()


def _run_import_command(settings: Settings, args: argparse.Namespace) -> int:
    """Validate the import's required inputs, take a verified read-only
    snapshot of both legacy sources, then import the outage log and
    samples and print the resulting report.

    With ``--dry-run``, the same import runs for real against a throwaway
    database instead of the application's own, so the printed report is
    trustworthy without writing anything an operator has not yet decided
    to keep.
    """
    if not args.serial:
        print("--serial is required (no device serial given)", file=sys.stderr)
        return 2
    device_config = next((d for d in settings.devices if d.serial == args.serial), None)
    if device_config is None:
        print(f"{args.serial!r} is not a configured device serial", file=sys.stderr)
        return 2
    if not args.source_tz:
        print(
            "--source-tz is required (the timezone outages.log was written in)",
            file=sys.stderr,
        )
        return 2

    try:
        snapshot = make_snapshot(
            samples_db=Path(args.samples),
            outage_log=Path(args.outage_log),
            snapshot_dir=settings.data_dir / "import-tmp",
        )
    except SnapshotError as exc:
        print(f"import snapshot failed: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    adapter_id = device_config.adapter_id or "generic"
    if args.dry_run:
        with tempfile.TemporaryDirectory() as scratch_dir:
            report = _import_into(
                db_path=Path(scratch_dir) / "dry-run.db",
                serial=args.serial,
                adapter_id=adapter_id,
                source_tz=args.source_tz,
                snapshot_samples_db=snapshot.samples_db,
                snapshot_outage_log=snapshot.outage_log,
                now=now,
                record_run=False,
            )
        print(report.render())
        print("(dry run: nothing was written to the application database)")
    else:
        report = _import_into(
            db_path=settings.data_dir / "ecoflow-stats.db",
            serial=args.serial,
            adapter_id=adapter_id,
            source_tz=args.source_tz,
            snapshot_samples_db=snapshot.samples_db,
            snapshot_outage_log=snapshot.outage_log,
            now=now,
            record_run=True,
        )
        print(report.render())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, validate configuration, then dispatch.

    Returns the process exit code for the configuration-error path (``2``);
    ``serve``, ``check``, ``import`` and ``healthcheck`` return their own
    real exit codes. ``recompute`` raises ``NotImplementedError`` until its
    own work unit lands.
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

    if args.command == "import":
        return _run_import_command(settings, args)

    if args.command == "healthcheck":
        return run_healthcheck(settings)

    raise NotImplementedError(f"{args.command!r} is not implemented yet")


__all__ = ["main"]
