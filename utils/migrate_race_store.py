#!/usr/bin/env python3
"""Rewrite closed Apex Engineer packet stores using compressed JSON BLOBs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.f1telemetry.race_store import migrate_packet_storage


def database_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_file():
        return candidate
    session = ROOT / "data" / "sessions" / value / "race-data.sqlite3"
    if session.is_file():
        return session
    raise argparse.ArgumentTypeError(f"no race database found for {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=database_path, help="session ID or race-data.sqlite3 path")
    parser.add_argument(
        "--discard-original", action="store_true",
        help="remove the verified uncompressed backup after the atomic replacement",
    )
    args = parser.parse_args()
    result = migrate_packet_storage(args.database, keep_backup=not args.discard_original)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
