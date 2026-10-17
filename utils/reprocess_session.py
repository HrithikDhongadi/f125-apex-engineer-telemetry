#!/usr/bin/env python3
"""Regenerate a versioned Markdown report from immutable stored telemetry."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.f1telemetry.application.offline_reports import OfflineReportService  # noqa: E402
from src.f1telemetry.reports import ExportError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("output", type=Path)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--report-type", choices=("auto", "race", "time_trial", "lap_analysis"), default="auto")
    args = parser.parse_args()

    reports = OfflineReportService(args.data_dir)
    try:
        _name, _mime, body = reports.export_markdown(args.session_id, args.report_type)
    except ExportError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(body)
    temporary.replace(args.output)
    print(f"Wrote {args.output} ({len(body)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
