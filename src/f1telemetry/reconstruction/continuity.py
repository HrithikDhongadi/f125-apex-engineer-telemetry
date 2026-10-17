"""Read-only packet continuity and setup-provenance analysis."""

from __future__ import annotations

import sqlite3
from typing import Any


def analyze_continuity(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    from ..storage.packets import _continuity_query

    return _continuity_query(connection)


def summarize_store(connection: sqlite3.Connection) -> dict[str, Any]:
    from ..storage.packets import _summary_query

    return _summary_query(connection)
