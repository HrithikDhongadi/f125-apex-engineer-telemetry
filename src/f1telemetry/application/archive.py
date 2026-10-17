"""Session/lap JSON repository and compatibility readers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..domain.models import LEGACY_SESSION_ID, Lap, RecordingSession, enrich_lap


OPEN_STATUSES = {"armed", "recording"}


@dataclass
class ArchiveContents:
    sessions: dict[str, RecordingSession]
    session_laps: dict[str, list[Lap]]
    selected_session_id: str

    @property
    def laps(self) -> list[Lap]:
        return [lap for laps in self.session_laps.values() for lap in laps]


class SessionArchiveRepository:
    """Read and write existing session manifests and full-fidelity lap files."""

    def __init__(self, capture_dir: Path) -> None:
        self.capture_dir = capture_dir
        self.sessions_dir = capture_dir / "sessions"
        self.notes_path = capture_dir / "notes.json"
        self.index_path = capture_dir / "archive-index.json"
        self._index = self._load_index()
        self._index_dirty = False

    def _load_index(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            return payload.get("entries", {}) if payload.get("version") == 1 else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_index(self) -> None:
        if not self._index_dirty:
            return
        self.atomic_json(self.index_path, {"version": 1, "entries": self._index}, compact=True)
        self._index_dirty = False

    def _lap_payload(self, path: Path, *, metadata_only: bool) -> dict[str, Any]:
        relative = str(path.relative_to(self.capture_dir))
        stat = path.stat()
        cached = self._index.get(relative)
        if metadata_only and isinstance(cached, dict) and cached.get("size") == stat.st_size and cached.get("mtime_ns") == stat.st_mtime_ns:
            payload = cached.get("payload")
            if isinstance(payload, dict):
                return dict(payload)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("lap JSON must be an object")
        metadata = {key: value for key, value in payload.items() if key != "samples"}
        metadata["sample_count"] = int(payload.get("sample_count") or len(payload.get("samples") or []))
        self._index[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "payload": metadata}
        self._index_dirty = True
        return metadata if metadata_only else payload

    @staticmethod
    def lap_from_payload(payload: dict[str, Any], defaults: dict[str, Any] | None = None) -> Lap:
        allowed = {item.name for item in fields(Lap)}
        values = dict(defaults or {})
        values.update({key: value for key, value in payload.items() if key in allowed})
        lap = enrich_lap(Lap(**values))
        lap._sample_count = int(payload.get("sample_count") or len(lap.samples))
        return lap

    @staticmethod
    def session_from_payload(payload: dict[str, Any]) -> RecordingSession:
        allowed = {item.name for item in fields(RecordingSession)}
        return RecordingSession(**{key: value for key, value in payload.items() if key in allowed})

    def load_notes(self) -> dict[str, str]:
        try:
            payload = json.loads(self.notes_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in payload.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def save_notes(self, notes: dict[str, str]) -> None:
        self.notes_path.write_text(json.dumps(notes, indent=2, sort_keys=True), encoding="utf-8")

    def load_session_directory(self, directory: Path, *, lazy_traces: bool = False) -> tuple[RecordingSession, list[Lap]]:
        payload = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        session = self.session_from_payload(payload)
        laps = []
        defaults = {
            "recording_session_id": session.id,
            "game_session_uid": session.game_session_uid,
            "mode": session.mode,
            "track_id": session.track_id,
            "track_length_m": session.track_length_m,
        }
        for lap_path in sorted(directory.glob("lap-*.json")):
            lap_payload = self._lap_payload(lap_path, metadata_only=lazy_traces)
            laps.append(self.lap_from_payload(lap_payload, defaults))
        session.lap_ids = [lap.id for lap in laps]
        return session, laps

    def load(
        self,
        *,
        mark_open_interrupted: bool,
        interrupted_at: Callable[[], str] | None = None,
        lazy_traces: bool = False,
    ) -> ArchiveContents:
        sessions: dict[str, RecordingSession] = {}
        session_laps: dict[str, list[Lap]] = {}
        legacy_laps: list[Lap] = []
        for path in sorted(self.capture_dir.glob("lap-*.json")):
            try:
                payload = self._lap_payload(path, metadata_only=lazy_traces)
                legacy_laps.append(self.lap_from_payload(payload, {"recording_session_id": LEGACY_SESSION_ID}))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        sessions[LEGACY_SESSION_ID] = RecordingSession(
            LEGACY_SESSION_ID, "Legacy captures", "unknown", "", None, "legacy", None, None, None,
            [lap.id for lap in legacy_laps],
        )
        session_laps[LEGACY_SESSION_ID] = legacy_laps

        for metadata_path in sorted(self.sessions_dir.glob("*/session.json")):
            try:
                session, laps = self.load_session_directory(metadata_path.parent, lazy_traces=lazy_traces)
                if mark_open_interrupted and session.status in OPEN_STATUSES:
                    session.status = "interrupted"
                    session.ended_at = interrupted_at() if interrupted_at else session.ended_at
                    self.persist_session(session)
                sessions[session.id] = session
                session_laps[session.id] = laps
            except (OSError, ValueError, TypeError, KeyError):
                continue

        real_sessions = [session for session in sessions.values() if session.id != LEGACY_SESSION_ID]
        selected = max(real_sessions, key=lambda item: item.started_at).id if real_sessions else LEGACY_SESSION_ID
        self._save_index()
        return ArchiveContents(sessions, session_laps, selected)

    def persist_session(self, session: RecordingSession) -> None:
        directory = self.sessions_dir / session.id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "session.json.tmp"
        temporary.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        temporary.replace(directory / "session.json")

    @staticmethod
    def atomic_json(path: Path, payload: dict[str, Any], compact: bool = False) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")) if compact else json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def lap_path(self, lap: Lap) -> Path:
        if lap.recording_session_id == LEGACY_SESSION_ID:
            return self.capture_dir / f"{lap.id}.json"
        return self.sessions_dir / lap.recording_session_id / f"{lap.id}.json"

    def raw_lap(self, lap: Lap) -> bytes | None:
        try:
            return self.lap_path(lap).read_bytes()
        except OSError:
            return None

    def load_full_lap(self, lap: Lap) -> Lap:
        payload = self._lap_payload(self.lap_path(lap), metadata_only=False)
        return self.lap_from_payload(payload, {
            "recording_session_id": lap.recording_session_id,
            "game_session_uid": lap.game_session_uid,
            "mode": lap.mode,
            "track_id": lap.track_id,
            "track_length_m": lap.track_length_m,
        })

    def invalidate_lap_index(self, lap: Lap) -> None:
        try:
            relative = str(self.lap_path(lap).relative_to(self.capture_dir))
        except ValueError:
            return
        if self._index.pop(relative, None) is not None:
            self._index_dirty = True
            self._save_index()
