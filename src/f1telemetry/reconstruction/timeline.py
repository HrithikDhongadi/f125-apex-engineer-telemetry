"""Flashback-branch reconstruction and player event attribution."""

from __future__ import annotations

import sqlite3
from typing import Any


def reconstruct_timeline(
    connection: sqlite3.Connection,
    lap_from: int | None = None,
    lap_to: int | None = None,
) -> list[dict[str, Any]]:
    """Derive accepted/superseded/unknown branches without mutating storage."""
    # Kept behind this explicit boundary while historical correlation remains
    # byte-for-byte compatible with the characterized v2 implementation.
    from ..storage.packets import _timeline_query

    return _timeline_query(connection, lap_from, lap_to)
