#!/usr/bin/env python3
"""
Build a combined, time-aligned experiment bundle.

The bundle is what you actually analyse: every role's telemetry joined onto one time
base, with the event stream overlaid and the clock corrections recorded. See
``docs/DATA-FORMAT.md`` §6 and ``gateway/aqua_gateway/export/bundle.py``.

Usage:
    python tools/export-experiment.py --list
    python tools/export-experiment.py --experiment EXP-2026-10-09-01
    python tools/export-experiment.py --experiment EXP-2026-10-09-01 --grid 0.2
    python tools/export-experiment.py --experiment EXP-2026-10-09-01 --out /tmp/bundles --no-compress

Runs against the local archive and database, so it works on the Pi and — with the
same ``.env`` — from a laptop that has synced the archive down from object storage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "gateway"))

from aqua_gateway.archive.manifest import Manifest  # noqa: E402
from aqua_gateway.config import get_settings  # noqa: E402
from aqua_gateway.db.engine import Database  # noqa: E402
from aqua_gateway.export.bundle import ExportError, build_bundle  # noqa: E402
from aqua_gateway.schema.registry import load_registry  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    registry = load_registry()
    manifest = Manifest(settings.manifest_path)

    db = Database(settings)
    if not await db.connect():
        print(
            "database unavailable; the export needs the experiment record and events",
            file=sys.stderr,
        )
        manifest.close()
        return 2

    try:
        if args.list:
            rows = await db.fetch(
                "SELECT id, title, status, roles, started_at, ended_at "
                "FROM experiments ORDER BY COALESCE(started_at, created_at) DESC LIMIT 100"
            )
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
            else:
                if not rows:
                    print("no experiments registered")
                for row in rows:
                    started = (
                        row["started_at"].isoformat()
                        if row["started_at"]
                        else "(not started)"
                    )
                    print(
                        f"  {row['id']:<28} {row['status']:<10} {started}  {row.get('title') or ''}"
                    )
            return 0

        out_dir = (
            Path(args.out) / args.experiment
            if args.out
            else settings.export_root / args.experiment
        )

        report = await build_bundle(
            settings,
            manifest,
            db,
            registry,
            args.experiment,
            out_dir=out_dir,
            grid_seconds=args.grid,
            max_hold_s=args.max_hold,
            compress=not args.no_compress,
        )
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close()
        manifest.close()

    if args.json:
        print(
            json.dumps(
                {
                    "experiment_id": report.experiment_id,
                    "out_dir": str(report.out_dir),
                    "bundle": str(report.bundle_path)
                    if report.bundle_path
                    else None,
                    "rows": report.rows,
                    "columns": report.columns,
                    "events": report.events,
                    "grid_seconds": report.grid_seconds,
                    "roles_present": report.roles_present,
                    "roles_missing": report.roles_missing,
                    "sources_unavailable": report.sources_unavailable,
                    "warnings": report.warnings,
                },
                indent=2,
            )
        )
    else:
        print(report.summary())
        print(f"  output: {report.out_dir}")
        if report.bundle_path:
            size_mb = report.bundle_path.stat().st_size / 1024**2
            print(f"  bundle: {report.bundle_path.name} ({size_mb:.2f} MiB)")
        if report.roles_missing:
            # Not fatal, but the caller needs to know the analysis will be missing a node.
            print(
                f"  missing roles: {', '.join(report.roles_missing)} — retrieve the archive and re-run"
            )
        for warning in report.warnings:
            print(f"  warning: {warning}")

    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a combined, time-aligned experiment bundle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--experiment", "-e", help="experiment id, e.g. EXP-2026-10-09-01"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list registered experiments and exit",
    )
    parser.add_argument(
        "--out",
        help="output directory (default: AQUA_EXPORT_ROOT/<experiment>)",
    )
    parser.add_argument(
        "--grid",
        type=float,
        default=1.0,
        help="common time grid in seconds (default 1.0; use 0.2 for the native 5 Hz)",
    )
    parser.add_argument(
        "--max-hold",
        type=float,
        default=60.0,
        help=(
            "how long a carried-forward value stays valid, in seconds (default 60). "
            "Past this the cell is left empty — an empty cell means no reading was current, "
            "not a reading of zero"
        ),
    )
    parser.add_argument(
        "--no-compress", action="store_true", help="skip building the .tar.gz"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="debug logging"
    )
    args = parser.parse_args(argv)

    if not args.list and not args.experiment:
        parser.error("--experiment is required unless --list is given")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
