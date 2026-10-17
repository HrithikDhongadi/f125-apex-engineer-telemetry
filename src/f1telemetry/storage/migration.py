"""Closed-store packet encoding migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def migrate_packet_storage(path: Path, keep_backup: bool = True) -> dict[str, Any]:
    # Imported lazily so the writer can initialize without a migration cycle.
    from .packets import _migrate_packet_storage

    return _migrate_packet_storage(path, keep_backup)
