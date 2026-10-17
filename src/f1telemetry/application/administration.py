"""Recoverable session trash and validated settings workflows."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from typing import Any, Callable
from uuid import uuid4

from ..domain.errors import SessionStateError
from ..domain.models import LEGACY_SESSION_ID, Lap, RecordingSession
from ..settings import SettingsStore
from .archive import OPEN_STATUSES, SessionArchiveRepository


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class SessionTrashService:
    def __init__(self, archive: SessionArchiveRepository, now: Callable[[], str]) -> None:
        self.archive = archive
        self.capture_dir = archive.capture_dir
        self.sessions_dir = archive.sessions_dir
        self.trash_dir = self.capture_dir / "trash" / "sessions"
        self.now = now

    def delete(
        self,
        session: RecordingSession | None,
        laps: list[Lap],
        *,
        active_recording_id: str | None,
        confirmed: bool,
    ) -> dict[str, Any]:
        if session is None:
            raise SessionStateError("Unknown session ID")
        if not confirmed:
            raise SessionStateError("Session deletion must be explicitly confirmed")
        if session.id == LEGACY_SESSION_ID:
            raise SessionStateError("Legacy captures cannot be deleted")
        if session.id == active_recording_id or session.status in OPEN_STATUSES:
            raise SessionStateError("Stop the active recording before deleting this session")
        source = self.sessions_dir / session.id
        if not source.is_dir():
            raise SessionStateError("Session directory is unavailable; nothing was deleted")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.trash_dir / f"{session.id}--deleted-{stamp}-{uuid4().hex[:8]}"
        try:
            self.trash_dir.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
        except OSError as error:
            raise SessionStateError("Could not move the session to local trash") from error
        try:
            self.archive.atomic_json(destination / "deletion.json", {
                "deleted_at": self.now(),
                "original_path": str(Path("sessions") / session.id),
                "session": asdict(session),
                "lap_ids": [lap.id for lap in laps],
                "personal_bests_preserved": True,
                "notes_preserved": True,
            })
        except OSError as error:
            destination.replace(source)
            raise SessionStateError("Could not create a recoverable deletion record") from error
        return {
            "deleted_session_id": session.id,
            "recoverable": True,
            "moved_to": str(destination.relative_to(self.capture_dir)),
            "personal_bests_preserved": True,
            "notes_preserved": True,
        }

    def list(self, retention_days: int) -> list[dict[str, Any]]:
        result = []
        paths = sorted(self.trash_dir.glob("*"), reverse=True) if self.trash_dir.exists() else []
        for path in paths:
            if not path.is_dir():
                continue
            try:
                deletion = json.loads((path / "deletion.json").read_text(encoding="utf-8"))
                session = deletion.get("session") or json.loads((path / "session.json").read_text(encoding="utf-8"))
                deleted_at = _parse_utc(deletion.get("deleted_at"))
                if deleted_at is None:
                    deleted_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                purge_at = deleted_at + timedelta(days=retention_days) if retention_days else None
                result.append({
                    "trash_id": path.name,
                    "session_id": session.get("id"),
                    "name": session.get("name", session.get("id", path.name)),
                    "mode": session.get("mode", "unknown"),
                    "track_id": session.get("track_id"),
                    "started_at": session.get("started_at"),
                    "deleted_at": deleted_at.isoformat().replace("+00:00", "Z"),
                    "purge_at": purge_at.isoformat().replace("+00:00", "Z") if purge_at else None,
                    "lap_count": len(deletion.get("lap_ids") or session.get("lap_ids") or []),
                })
            except (OSError, ValueError, TypeError):
                continue
        return result

    def restore(
        self,
        trash_id: str,
        *,
        confirmed: bool,
        existing_session_ids: set[str],
    ) -> tuple[RecordingSession, list[Lap]]:
        if not confirmed:
            raise SessionStateError("Session restoration must be explicitly confirmed")
        if not trash_id or Path(trash_id).name != trash_id:
            raise SessionStateError("Invalid deleted-session ID")
        source = self.trash_dir / trash_id
        if not source.is_dir():
            raise SessionStateError("Deleted session was not found; it may already have expired")
        try:
            payload = json.loads((source / "session.json").read_text(encoding="utf-8"))
            session = self.archive.session_from_payload(payload)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise SessionStateError("Deleted session metadata is unreadable") from error
        destination = self.sessions_dir / session.id
        if destination.exists() or session.id in existing_session_ids:
            raise SessionStateError(f"Cannot restore: session {session.id} already exists")
        source.replace(destination)
        try:
            return self.archive.load_session_directory(destination)
        except (OSError, ValueError, TypeError, KeyError) as error:
            destination.replace(source)
            raise SessionStateError("Could not load the restored session") from error

    def purge_expired(self, retention_days: int) -> dict[str, Any]:
        if retention_days == 0:
            return {"purged": [], "retention_days": 0}
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        purged = []
        for item in self.list(retention_days):
            deleted_at = _parse_utc(item.get("deleted_at"))
            if deleted_at is None or deleted_at > cutoff:
                continue
            target = self.trash_dir / item["trash_id"]
            if target.is_dir() and target.parent == self.trash_dir:
                shutil.rmtree(target)
                purged.append(item["trash_id"])
        return {"purged": purged, "retention_days": retention_days}


class RuntimeSettingsService:
    def __init__(self, store: SettingsStore, trash: SessionTrashService) -> None:
        self.store = store
        self.trash = trash

    def update(self, current: dict[str, Any], values: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        updated = self.store.save(values)
        purge = self.trash.purge_expired(int(updated["trash_retention_days"]))
        restart_fields = [
            key for key in ("web_host", "web_port", "udp_port")
            if current.get(key) != updated.get(key)
        ]
        return updated, {
            "settings": dict(updated),
            "restart_required": restart_fields,
            "purge": purge,
        }
