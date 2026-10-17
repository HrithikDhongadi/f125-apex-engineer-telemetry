"""Read-only report generation that never starts or mutates the recorder."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Any

from ..circuit_profiles import SILVERSTONE_SEED
from ..circuits import track_name
from ..race_store import RaceSessionStore
from ..reports import ExportError, build_markdown, safe_filename
from .archive import SessionArchiveRepository


def _read_profile(data_dir: Path, profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    if profile_id == SILVERSTONE_SEED["id"]:
        return deepcopy(SILVERSTONE_SEED)
    safe = "".join(character for character in profile_id if character.isalnum() or character in "-_")
    if not safe or safe != profile_id:
        return None
    path = data_dir / "circuit_profiles" / f"{safe}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


class OfflineReportService:
    """Regenerate Markdown from files without SessionStore startup side effects."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.archive = SessionArchiveRepository(data_dir)

    def export_markdown(self, session_id: str, report_type: str = "auto") -> tuple[str, str, bytes]:
        contents = self.archive.load(mark_open_interrupted=False)
        session = contents.sessions.get(session_id)
        if session is None:
            raise ExportError("Unknown session ID")
        laps = contents.session_laps[session_id]
        database = self.archive.sessions_dir / session.id / session.race_store_file if session.race_store_file else None
        if not laps and not (database and database.exists()):
            raise ExportError("This export contains no completed laps")
        timeline = None
        storage_summary = None
        if database and database.exists():
            lap_numbers = [lap.number for lap in laps]
            try:
                timeline = RaceSessionStore.read_timeline(
                    database,
                    min(lap_numbers) if lap_numbers else None,
                    max(lap_numbers) if lap_numbers else None,
                )
                storage_summary = RaceSessionStore.read_summary(database)
            except sqlite3.Error:
                storage_summary = {"path": session.race_store_file, "error": "unreadable"}
        profile = _read_profile(self.data_dir, session.circuit_profile_id)
        report = build_markdown(
            session,
            laps,
            self.archive.load_notes(),
            track_name(session.track_id, session.track_name_override),
            timeline,
            storage_summary,
            profile,
            report_type,
        )
        resolved_type = report_type if report_type != "auto" else (
            "race" if session.mode == "race" else
            "time_trial" if session.mode == "time_trial" else
            "lap_analysis"
        )
        base = safe_filename(f"{session.name}-{resolved_type.replace('_', '-')}")
        return f"{base}.md", "text/markdown; charset=utf-8", report.encode("utf-8")
