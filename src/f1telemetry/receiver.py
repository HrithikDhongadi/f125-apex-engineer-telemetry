"""UDP receiver, persistent recording sessions, and local lap storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import socket
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from .circuits import is_known_track, track_name
from .circuit_profiles import CircuitProfileError, CircuitProfileStore, profile_identity
from .protocol import (
    PACKET_CAR_DAMAGE, PACKET_CAR_SETUPS, PACKET_CAR_STATUS, PACKET_CAR_TELEMETRY,
    PACKET_EVENT, PACKET_FINAL_CLASSIFICATION, PACKET_LAP_DATA, PACKET_MOTION,
    PACKET_MOTION_EX, PACKET_SESSION, decode_event, decode_event_code, decode_header,
    decode_packet, decode_player_car_telemetry, decode_player_lap_data, decode_player_setup,
    decode_session, supported_header,
)
from .personal_bests import PersonalBestRegistry
from .quality import lap_quality
from .race_store import RaceSessionStore
from .setups import SetupValidationError, setup_rows, validate_manual_values, with_provenance
from .settings import SettingsStore

LEGACY_SESSION_ID = "legacy"
OPEN_STATUSES = {"armed", "recording"}
RACE_UID_REOPEN_GUARD_SECONDS = 5.0
MAX_DIAGNOSTIC_SEGMENTS = 8
MAX_DIAGNOSTIC_EVENTS = 50_000
MAX_DIAGNOSTIC_BYTES = 50 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Lap:
    number: int
    time_ms: int
    invalid: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    setup: dict[str, Any] | None = None
    saved_at: str = ""
    sector1_ms: int | None = None
    sector2_ms: int | None = None
    first_session_time: float | None = None
    last_session_time: float | None = None
    peak_speed_kph: int | None = None
    minimum_speed_kph: int | None = None
    max_brake_temps_c: list[int] | None = None
    max_tyre_inner_c: list[int] | None = None
    recording_session_id: str = LEGACY_SESSION_ID
    game_session_uid: int | None = None
    mode: str = "unknown"
    track_id: int | None = None
    track_length_m: int | None = None
    race_context: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return f"lap-{self.saved_at}-{self.number}"

    def summary(self, note: str = "") -> dict[str, Any]:
        return {
            "id": self.id, "number": self.number, "time_ms": self.time_ms,
            "invalid": self.invalid, "sample_count": len(self.samples),
            "event_count": len(self.events), "events": self.events,
            "setup": self.setup, "saved_at": self.saved_at,
            "sector1_ms": self.sector1_ms, "sector2_ms": self.sector2_ms,
            "first_session_time": self.first_session_time,
            "last_session_time": self.last_session_time,
            "peak_speed_kph": self.peak_speed_kph,
            "minimum_speed_kph": self.minimum_speed_kph,
            "max_brake_temps_c": self.max_brake_temps_c,
            "max_tyre_inner_c": self.max_tyre_inner_c, "note": note,
            "recording_session_id": self.recording_session_id,
            "game_session_uid": self.game_session_uid, "mode": self.mode,
            "track_id": self.track_id, "track_length_m": self.track_length_m,
            "quality": lap_quality(self),
        }


@dataclass
class RecordingSession:
    id: str
    name: str
    mode: str
    started_at: str
    ended_at: str | None
    status: str
    game_session_uid: int | None
    track_id: int | None
    track_length_m: int | None
    lap_ids: list[str] = field(default_factory=list)
    track_name_override: str | None = None
    packet_format: int = 2025
    sector2_start_m: float | None = None
    sector3_start_m: float | None = None
    circuit_profile_id: str | None = None
    race_store_file: str | None = None

    def summary(self, laps: list[Lap]) -> dict[str, Any]:
        valid = [lap.time_ms for lap in laps if not lap.invalid]
        return {
            **asdict(self), "lap_count": len(laps),
            "best_valid_lap_ms": min(valid) if valid else None,
            "track_name": track_name(self.track_id, self.track_name_override),
            "official_track_name": track_name(self.track_id) if is_known_track(self.track_id) else None,
            "can_override_track_name": not is_known_track(self.track_id),
            "read_only": self.id == LEGACY_SESSION_ID,
        }


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _wheel_max(samples: list[dict[str, Any]], key: str) -> list[int] | None:
    rows = [values for sample in samples if isinstance((values := sample.get(key)), (list, tuple)) and len(values) == 4]
    return [max(int(row[index]) for row in rows) for index in range(4)] if rows else None


def enrich_lap(lap: Lap) -> Lap:
    speeds = [int(sample["speed_kph"]) for sample in lap.samples if sample.get("speed_kph") is not None]
    times = [float(sample["t"]) for sample in lap.samples if sample.get("t") is not None]
    if lap.first_session_time is None and times:
        lap.first_session_time = min(times)
    if lap.last_session_time is None and times:
        lap.last_session_time = max(times)
    if lap.peak_speed_kph is None and speeds:
        lap.peak_speed_kph = max(speeds)
    if lap.minimum_speed_kph is None and speeds:
        lap.minimum_speed_kph = min(speeds)
    if lap.max_brake_temps_c is None:
        lap.max_brake_temps_c = _wheel_max(lap.samples, "brake_temps_c")
    if lap.max_tyre_inner_c is None:
        lap.max_tyre_inner_c = _wheel_max(lap.samples, "tyre_inner_c")
    return lap


class SessionStateError(ValueError):
    pass


class SessionStore:
    def __init__(self, capture_dir: Path) -> None:
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.capture_dir / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)
        self.notes_path = self.capture_dir / "notes.json"
        self.settings_store = SettingsStore(self.capture_dir / "settings.json")
        self.settings = self.settings_store.load()
        self.pb_registry = PersonalBestRegistry(self.capture_dir / "personal_bests")
        self.profile_store = CircuitProfileStore(self.capture_dir / "circuit_profiles")
        self.lock = Lock()
        self.latest: dict[str, Any] = {"connected": False}
        self.last_packet_at: float | None = None
        self.notes = self._load_notes()
        self.sessions: dict[str, RecordingSession] = {}
        self.session_laps: dict[str, list[Lap]] = {}
        self.laps: list[Lap] = []
        self.game_session_uid: int | None = None
        self.game_mode = "unknown"
        self.game_session_type: int | None = None
        self.track_id: int | None = None
        self.track_length_m: int | None = None
        self.sector2_start_m: float | None = None
        self.sector3_start_m: float | None = None
        self.active_recording_id: str | None = None
        self.selected_session_id: str | None = None
        self.manual_stop_uid: int | None = None
        self.race_uid_reopen_guard: dict[int, float] = {}
        self.current_setup: dict[str, Any] | None = None
        self.active_lap_number: int | None = None
        self.active_invalid = False
        self.active_samples: list[dict[str, Any]] = []
        self.active_lap_events: list[dict[str, Any]] = []
        self.pending_restart_event: dict[str, Any] | None = None
        self.active_lap_state: dict[str, Any] = {}
        self.active_lap_setup: dict[str, Any] | None = None
        self.capture_current_lap = False
        self.race_store: RaceSessionStore | None = None
        self.packet_state: dict[int, dict[str, Any]] = {}
        self.active_lap_context: dict[str, Any] | None = None
        self.diagnostics_enabled = False
        self.diagnostic_path: Path | None = None
        self.diagnostic_segment = -1
        self.diagnostic_events = 0
        self.diagnostic_bytes = 0
        self.diagnostic_truncated = False
        self._purge_expired_trash_unlocked()
        self._load_all()

    def _load_notes(self) -> dict[str, str]:
        try:
            payload = json.loads(self.notes_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in payload.items()}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _lap_from_payload(payload: dict[str, Any], defaults: dict[str, Any] | None = None) -> Lap:
        allowed = {item.name for item in fields(Lap)}
        values = dict(defaults or {})
        values.update({key: value for key, value in payload.items() if key in allowed})
        return enrich_lap(Lap(**values))

    def _load_all(self) -> None:
        legacy_laps: list[Lap] = []
        for path in sorted(self.capture_dir.glob("lap-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                legacy_laps.append(self._lap_from_payload(payload, {"recording_session_id": LEGACY_SESSION_ID}))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        self.sessions[LEGACY_SESSION_ID] = RecordingSession(
            LEGACY_SESSION_ID, "Legacy captures", "unknown", "", None, "legacy", None, None, None,
            [lap.id for lap in legacy_laps],
        )
        self.session_laps[LEGACY_SESSION_ID] = legacy_laps

        session_fields = {item.name for item in fields(RecordingSession)}
        for metadata_path in sorted(self.sessions_dir.glob("*/session.json")):
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                session = RecordingSession(**{key: value for key, value in payload.items() if key in session_fields})
                laps = []
                for lap_path in sorted(metadata_path.parent.glob("lap-*.json")):
                    lap_payload = json.loads(lap_path.read_text(encoding="utf-8"))
                    defaults = {
                        "recording_session_id": session.id, "game_session_uid": session.game_session_uid,
                        "mode": session.mode, "track_id": session.track_id, "track_length_m": session.track_length_m,
                    }
                    laps.append(self._lap_from_payload(lap_payload, defaults))
                session.lap_ids = [lap.id for lap in laps]
                if session.status in OPEN_STATUSES:
                    session.status, session.ended_at = "interrupted", utc_now()
                    self.sessions[session.id] = session
                    self.session_laps[session.id] = laps
                    self._persist_session(session)
                else:
                    self.sessions[session.id] = session
                    self.session_laps[session.id] = laps
            except (OSError, ValueError, TypeError, KeyError):
                continue
        self._refresh_lap_index()
        real_sessions = [session for session in self.sessions.values() if session.id != LEGACY_SESSION_ID]
        if real_sessions:
            self.selected_session_id = max(real_sessions, key=lambda item: item.started_at).id
        else:
            self.selected_session_id = LEGACY_SESSION_ID

    def _refresh_lap_index(self) -> None:
        self.laps = [lap for laps in self.session_laps.values() for lap in laps]

    def _persist_session(self, session: RecordingSession) -> None:
        directory = self.sessions_dir / session.id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "session.json.tmp"
        temporary.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        temporary.replace(directory / "session.json")

    def _touch(self) -> None:
        self.latest["connected"] = True
        self.last_packet_at = monotonic()

    def record_receiver_error(self, error: Exception) -> None:
        """Expose a packet failure without allowing the UDP thread to die silently."""
        with self.lock:
            count = int(self.latest.get("receiver_error_count", 0)) + 1
            self.latest.update({
                "receiver_error_count": count,
                "receiver_error": f"{type(error).__name__}: {error}",
                "receiver_error_at": utc_now(),
            })

    def _observe_uid(self, session_uid: int | None) -> None:
        if session_uid is None:
            return
        if self.game_session_uid is None:
            self.game_session_uid = session_uid
            return
        if session_uid == self.game_session_uid:
            return
        previous_uid = self.game_session_uid
        active = self.sessions.get(self.active_recording_id or "")
        self._close_active("completed")
        if active is not None and active.mode == "race" and previous_uid is not None:
            # At race finish F1 25 can emit a momentary different UID before a
            # final bundle from the completed UID. Do not let that trailing
            # Session packet create a second empty automatic recording.
            self.race_uid_reopen_guard[previous_uid] = monotonic() + RACE_UID_REOPEN_GUARD_SECONDS
        self._reset_lap()
        self.game_session_uid = session_uid
        self.game_mode, self.game_session_type = "unknown", None
        self.track_id, self.track_length_m = None, None
        self.sector2_start_m, self.sector3_start_m = None, None
        self.packet_state = {}
        self.current_setup = None
        self.manual_stop_uid = None
        for key in (
            "setup", "front_wing", "rear_wing", "on_throttle_diff", "off_throttle_diff",
            "lap_number", "current_lap_ms", "lap_distance_m", "invalid",
        ):
            self.latest.pop(key, None)

    def record_game_session(self, game: dict[str, Any]) -> None:
        with self.lock:
            self._observe_uid(int(game["session_uid"]))
            self.game_session_uid = int(game["session_uid"])
            self.game_mode = str(game["mode"])
            self.game_session_type = int(game["session_type"])
            self.track_id = int(game["track_id"])
            self.track_length_m = int(game["track_length_m"])
            self.sector2_start_m = float(game["sector2_start_m"]) if game.get("sector2_start_m") is not None else None
            self.sector3_start_m = float(game["sector3_start_m"]) if game.get("sector3_start_m") is not None else None
            self.latest.update({
                "game_session_uid": self.game_session_uid, "game_mode": self.game_mode,
                "session_type": self.game_session_type, "track_id": self.track_id,
                "track_length_m": self.track_length_m, "track_name": track_name(self.track_id),
            })
            self._touch()
            now = monotonic()
            self.race_uid_reopen_guard = {
                uid: expires for uid, expires in self.race_uid_reopen_guard.items()
                if expires > now
            }
            guarded_tail = self.race_uid_reopen_guard.get(self.game_session_uid, 0) > now
            if (
                self.game_mode == "race"
                and self.active_recording_id is None
                and self.manual_stop_uid != self.game_session_uid
                and not guarded_tail
            ):
                self._create_recording(f"Race · {track_name(self.track_id)}", "race", automatic=True)

    def game_session_ended(self, session_uid: int) -> None:
        with self.lock:
            if self.game_session_uid == session_uid:
                self._close_active("completed")
                self._reset_lap()
                self.manual_stop_uid = session_uid

    def _at_lap_start(self) -> bool:
        if self.active_lap_number is None:
            return False
        return (
            float(self.active_lap_state.get("current_lap_ms", 999_999)) <= 1_500
            and float(self.active_lap_state.get("lap_distance_m", 999_999)) <= 100
        )

    def _create_recording(self, name: str, mode: str, automatic: bool = False) -> RecordingSession:
        if self.active_recording_id is not None:
            raise SessionStateError("A recording session is already active")
        now = datetime.now(timezone.utc)
        session_id = f"session-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        at_start = self._at_lap_start()
        session = RecordingSession(
            id=session_id, name=name.strip()[:80] or ("Time Trial run" if mode == "time_trial" else "Race"),
            mode=mode, started_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            ended_at=None, status="recording" if at_start else "armed",
            game_session_uid=self.game_session_uid, track_id=self.track_id,
            track_length_m=self.track_length_m, lap_ids=[],
            sector2_start_m=self.sector2_start_m, sector3_start_m=self.sector3_start_m,
            circuit_profile_id=(profile_identity(2025, self.track_id) if self.track_id is not None else None),
            race_store_file="race-data.sqlite3",
        )
        self.sessions[session.id], self.session_laps[session.id] = session, []
        self.active_recording_id = session.id
        if not automatic:
            self.selected_session_id = session.id
        elif self.selected_session_id is None:
            self.selected_session_id = session.id
        self.capture_current_lap = at_start
        self.active_samples = []
        self.active_lap_events = []
        self.pending_restart_event = None
        if at_start and self.active_lap_number is not None:
            self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
            self.active_invalid = bool(self.active_lap_state.get("invalid", False))
        self._persist_session(session)
        self.race_store = RaceSessionStore(
            self.sessions_dir / session.id / "race-data.sqlite3",
            max_bytes=int(float(self.settings["max_recording_gb"]) * 1024**3),
        )
        self.race_store.set_metadata("recording_session_id", session.id)
        self.race_store.set_metadata("mode", session.mode)
        self.race_store.set_metadata("track_id", session.track_id)
        self.race_store.append_event(
            float(self.active_lap_state.get("session_time", 0) or 0), "recording_started",
            {"status": session.status, "capture_current_lap": self.capture_current_lap},
            "recorder", self.active_lap_number,
        )
        if self.diagnostics_enabled:
            self.diagnostic_path = None
            self.diagnostic_segment = -1
            self.diagnostic_events = 0
            self.diagnostic_bytes = 0
            self.diagnostic_truncated = False
            self._start_diagnostic_segment("run-start")
            self._write_diagnostic({
                "event": "recording_started", "timestamp_utc": self._diagnostic_timestamp(),
                "session_uid": self.game_session_uid, "recording_session_id": session.id,
                "recording_status": session.status, "capture_current_lap": self.capture_current_lap,
                "active_lap_number": self.active_lap_number, "sample_count": len(self.active_samples),
                "previous_packet": dict(self.active_lap_state),
                "reason": "new_recording_created_at_lap_start" if at_start else "new_recording_armed_midlap",
            })
        return session

    @staticmethod
    def _diagnostic_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def diagnostic_status(self) -> dict[str, Any]:
        return {
            "enabled": self.diagnostics_enabled,
            "active_file": str(self.diagnostic_path.relative_to(self.capture_dir)) if self.diagnostic_path else None,
            "segment": self.diagnostic_segment if self.diagnostic_path else None,
            "events": self.diagnostic_events,
            "bytes": self.diagnostic_bytes,
            "truncated": self.diagnostic_truncated,
            "limits": {
                "segments": MAX_DIAGNOSTIC_SEGMENTS,
                "events_per_segment": MAX_DIAGNOSTIC_EVENTS,
                "bytes_per_segment": int(self.settings["diagnostic_max_mb"]) * 1024 * 1024,
            },
        }

    def configure_diagnostics(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            self.diagnostics_enabled = bool(enabled)
            if not self.diagnostics_enabled:
                self.diagnostic_path = None
            elif self.active_recording_id is not None and self.diagnostic_path is None:
                self._start_diagnostic_segment("manual-enable")
                self._write_diagnostic({
                    "event": "diagnostics_enabled", "timestamp_utc": self._diagnostic_timestamp(),
                    "session_uid": self.game_session_uid, "recording_session_id": self.active_recording_id,
                    "recording_status": self.sessions[self.active_recording_id].status,
                    "capture_current_lap": self.capture_current_lap,
                    "active_lap_number": self.active_lap_number, "sample_count": len(self.active_samples),
                    "previous_packet": dict(self.active_lap_state), "reason": "operator_opt_in",
                })
            return self.diagnostic_status()

    def _start_diagnostic_segment(self, reason: str) -> None:
        if not self.diagnostics_enabled or self.active_recording_id is None:
            return
        next_segment = self.diagnostic_segment + 1
        if next_segment >= MAX_DIAGNOSTIC_SEGMENTS:
            self.diagnostic_truncated = True
            return
        self.diagnostic_segment = next_segment
        self.diagnostic_events = 0
        self.diagnostic_bytes = 0
        self.diagnostic_truncated = False
        directory = self.sessions_dir / self.active_recording_id / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(character if character.isalnum() or character == "-" else "-" for character in reason)
        self.diagnostic_path = directory / f"lap-data-{next_segment:02d}-{safe_reason}.jsonl"

    def _write_diagnostic(self, event: dict[str, Any]) -> None:
        if not self.diagnostics_enabled or self.active_recording_id is None or self.diagnostic_path is None or self.diagnostic_truncated:
            return
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        diagnostic_max_bytes = int(self.settings["diagnostic_max_mb"]) * 1024 * 1024
        if self.diagnostic_events >= MAX_DIAGNOSTIC_EVENTS or self.diagnostic_bytes + len(line) > diagnostic_max_bytes:
            self.diagnostic_truncated = True
            marker = json.dumps({
                "event": "diagnostic_limit_reached", "timestamp_utc": self._diagnostic_timestamp(),
                "events": self.diagnostic_events, "bytes": self.diagnostic_bytes,
            }, separators=(",", ":")) + "\n"
            with self.diagnostic_path.open("ab") as stream:
                stream.write(marker.encode("utf-8"))
            return
        with self.diagnostic_path.open("ab") as stream:
            stream.write(line)
        self.diagnostic_events += 1
        self.diagnostic_bytes += len(line)

    def start_time_trial_run(self, name: str) -> dict[str, Any]:
        with self.lock:
            if self.game_mode != "time_trial" or self.game_session_uid is None:
                raise SessionStateError("Start new run is available only after a Time Trial Session packet is received")
            session = self._create_recording(name, "time_trial")
            return session.summary([])

    def stop_recording(self) -> dict[str, Any]:
        with self.lock:
            if self.active_recording_id is None:
                raise SessionStateError("No recording session is active")
            session = self.sessions[self.active_recording_id]
            if session.mode == "race":
                self.manual_stop_uid = session.game_session_uid
            self._close_active("stopped")
            self.active_samples = []
            self.active_lap_events = []
            self.pending_restart_event = None
            self.capture_current_lap = False
            return session.summary(self.session_laps[session.id])

    def interrupt_active(self) -> None:
        with self.lock:
            self._close_active("interrupted")

    def _close_active(self, status: str) -> None:
        if self.active_recording_id is None:
            return
        session = self.sessions[self.active_recording_id]
        session.status, session.ended_at = status, utc_now()
        if self.race_store is not None:
            if self.active_lap_number is not None:
                self.race_store.append_event(
                    float(self.latest.get("t", 0) or 0), "recording_closed_with_incomplete_lap",
                    {"status": status, "samples_in_lap_json_buffer": len(self.active_samples), "capture_current_lap": self.capture_current_lap},
                    "recorder", self.active_lap_number,
                )
            self.race_store.set_metadata("ended_at", session.ended_at)
            self.race_store.set_metadata("status", status)
            self.race_store.close()
            self.race_store = None
        self._persist_session(session)
        self.active_recording_id = None
        self.capture_current_lap = False
        self.active_samples = []
        self.active_lap_events = []
        self.pending_restart_event = None

    def rename_session(self, session_id: str, name: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise SessionStateError("Unknown session ID")
            if session.id == LEGACY_SESSION_ID:
                raise SessionStateError("Legacy captures are read-only")
            clean = name.strip()[:80]
            if not clean:
                raise SessionStateError("Session name cannot be empty")
            session.name = clean
            self._persist_session(session)
            return session.summary(self.session_laps[session.id])

    def delete_session(self, session_id: str, confirmed: bool = False) -> dict[str, Any]:
        """Remove a closed session from the library by atomically moving it to local trash."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise SessionStateError("Unknown session ID")
            if not confirmed:
                raise SessionStateError("Session deletion must be explicitly confirmed")
            if session.id == LEGACY_SESSION_ID:
                raise SessionStateError("Legacy captures cannot be deleted")
            if session.id == self.active_recording_id or session.status in OPEN_STATUSES:
                raise SessionStateError("Stop the active recording before deleting this session")

            source = self.sessions_dir / session.id
            if not source.is_dir():
                raise SessionStateError("Session directory is unavailable; nothing was deleted")
            trash_dir = self.capture_dir / "trash" / "sessions"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = trash_dir / f"{session.id}--deleted-{stamp}-{uuid4().hex[:8]}"
            try:
                trash_dir.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
            except OSError as error:
                raise SessionStateError("Could not move the session to local trash") from error
            try:
                self._atomic_json(destination / "deletion.json", {
                    "deleted_at": utc_now(),
                    "original_path": str(Path("sessions") / session.id),
                    "session": asdict(session),
                    "lap_ids": [lap.id for lap in self.session_laps.get(session.id, [])],
                    "personal_bests_preserved": True,
                    "notes_preserved": True,
                })
            except OSError as error:
                destination.replace(source)
                raise SessionStateError("Could not create a recoverable deletion record") from error

            self.sessions.pop(session.id)
            self.session_laps.pop(session.id, None)
            self._refresh_lap_index()
            if self.selected_session_id == session.id:
                remaining = [item for item in self.sessions.values() if item.id != LEGACY_SESSION_ID]
                self.selected_session_id = (
                    max(remaining, key=lambda item: item.started_at).id
                    if remaining else LEGACY_SESSION_ID
                )
            return {
                "deleted_session_id": session.id,
                "recoverable": True,
                "moved_to": str(destination.relative_to(self.capture_dir)),
                "selected_session_id": self.selected_session_id,
                "personal_bests_preserved": True,
                "notes_preserved": True,
            }

    @staticmethod
    def _parse_utc(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    def list_deleted_sessions(self) -> list[dict[str, Any]]:
        trash_dir = self.capture_dir / "trash" / "sessions"
        retention = int(self.settings["trash_retention_days"])
        result = []
        for path in sorted(trash_dir.glob("*"), reverse=True) if trash_dir.exists() else []:
            if not path.is_dir():
                continue
            try:
                deletion = json.loads((path / "deletion.json").read_text(encoding="utf-8"))
                session = deletion.get("session") or json.loads((path / "session.json").read_text(encoding="utf-8"))
                deleted_at = self._parse_utc(deletion.get("deleted_at"))
                if deleted_at is None:
                    deleted_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                purge_at = deleted_at + timedelta(days=retention) if retention else None
                result.append({
                    "trash_id": path.name, "session_id": session.get("id"),
                    "name": session.get("name", session.get("id", path.name)),
                    "mode": session.get("mode", "unknown"), "track_id": session.get("track_id"),
                    "started_at": session.get("started_at"), "deleted_at": deleted_at.isoformat().replace("+00:00", "Z"),
                    "purge_at": purge_at.isoformat().replace("+00:00", "Z") if purge_at else None,
                    "lap_count": len(deletion.get("lap_ids") or session.get("lap_ids") or []),
                })
            except (OSError, ValueError, TypeError):
                continue
        return result

    def restore_deleted_session(self, trash_id: str, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise SessionStateError("Session restoration must be explicitly confirmed")
        if not trash_id or Path(trash_id).name != trash_id:
            raise SessionStateError("Invalid deleted-session ID")
        with self.lock:
            source = self.capture_dir / "trash" / "sessions" / trash_id
            if not source.is_dir():
                raise SessionStateError("Deleted session was not found; it may already have expired")
            try:
                payload = json.loads((source / "session.json").read_text(encoding="utf-8"))
                session_fields = {item.name for item in fields(RecordingSession)}
                session = RecordingSession(**{key: value for key, value in payload.items() if key in session_fields})
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise SessionStateError("Deleted session metadata is unreadable") from error
            destination = self.sessions_dir / session.id
            if destination.exists() or session.id in self.sessions:
                raise SessionStateError(f"Cannot restore: session {session.id} already exists")
            source.replace(destination)
            try:
                laps = []
                for lap_path in sorted(destination.glob("lap-*.json")):
                    lap_payload = json.loads(lap_path.read_text(encoding="utf-8"))
                    laps.append(self._lap_from_payload(lap_payload, {
                        "recording_session_id": session.id, "game_session_uid": session.game_session_uid,
                        "mode": session.mode, "track_id": session.track_id, "track_length_m": session.track_length_m,
                    }))
                session.lap_ids = [lap.id for lap in laps]
                self.sessions[session.id], self.session_laps[session.id] = session, laps
                self.selected_session_id = session.id
                self._refresh_lap_index()
            except (OSError, ValueError, TypeError, KeyError) as error:
                destination.replace(source)
                raise SessionStateError("Could not load the restored session") from error
            return {"restored_session_id": session.id, "selected_session_id": session.id, "lap_count": len(laps)}

    def _purge_expired_trash_unlocked(self) -> dict[str, Any]:
        retention = int(self.settings.get("trash_retention_days", 30))
        if retention == 0:
            return {"purged": [], "retention_days": 0}
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        purged = []
        trash_dir = self.capture_dir / "trash" / "sessions"
        for item in self.list_deleted_sessions():
            deleted_at = self._parse_utc(item.get("deleted_at"))
            if deleted_at is None or deleted_at > cutoff:
                continue
            target = trash_dir / item["trash_id"]
            if target.is_dir() and target.parent == trash_dir:
                shutil.rmtree(target)
                purged.append(item["trash_id"])
        return {"purged": purged, "retention_days": retention}

    def purge_expired_trash(self) -> dict[str, Any]:
        with self.lock:
            return self._purge_expired_trash_unlocked()

    def update_settings(self, values: Any) -> dict[str, Any]:
        with self.lock:
            previous = dict(self.settings)
            self.settings = self.settings_store.save(values)
            purge = self._purge_expired_trash_unlocked()
            restart_fields = [key for key in ("web_host", "web_port", "udp_port") if previous.get(key) != self.settings.get(key)]
            return {"settings": dict(self.settings), "restart_required": restart_fields, "purge": purge}

    def override_track_name(self, session_id: str, name: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise SessionStateError("Unknown session ID")
            if session.id == LEGACY_SESSION_ID:
                raise SessionStateError("Legacy captures are read-only")
            if is_known_track(session.track_id):
                raise SessionStateError("Official circuit names cannot be overridden")
            clean = name.strip()[:80]
            if not clean:
                raise SessionStateError("Circuit display name cannot be empty")
            session.track_name_override = clean
            self._persist_session(session)
            return session.summary(self.session_laps[session.id])

    def select_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise SessionStateError("Unknown session ID")
            self.selected_session_id = session_id
            return session.summary(self.session_laps[session_id])

    def record_setup(
        self, setup: dict[str, Any], session_uid: int | None = None,
        packet_meta: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            decoded = with_provenance(setup, "decoded_udp") or {}
            if packet_meta:
                decoded["_capture"] = dict(packet_meta)
            self.current_setup = decoded
            if (
                self.capture_current_lap
                and float(self.active_lap_state.get("current_lap_ms", 0)) <= 1_500
            ):
                # Grid/pre-session setup packets can precede the applied race
                # setup. Keep refreshing only in the opening 1.5 s, then freeze
                # the lap snapshot so later setup packets cannot rewrite it.
                self.active_lap_setup = dict(decoded)
            self.latest.update(setup)
            self.latest["setup"] = dict(decoded)
            self._touch()

    def _reset_lap(self) -> None:
        self.active_lap_number = None
        self.active_invalid = False
        self.active_samples = []
        self.active_lap_events = []
        self.pending_restart_event = None
        self.active_lap_state = {}
        self.active_lap_setup = None
        self.active_lap_context = None
        self.capture_current_lap = False

    def _begin_lap(self, lap_number: int, capture: bool) -> None:
        self.active_lap_number = lap_number
        self.active_invalid = False
        self.active_samples = []
        self.active_lap_events = []
        self.active_lap_state = {}
        self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
        self.active_lap_context = self._current_race_context()
        self.capture_current_lap = capture

    def _current_race_context(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for packet_id, label in ((PACKET_SESSION, "session"), (PACKET_LAP_DATA, "lap"), (PACKET_CAR_STATUS, "car_status"), (PACKET_CAR_DAMAGE, "damage")):
            payload = self.packet_state.get(packet_id)
            if not isinstance(payload, dict):
                continue
            player = payload.get("player")
            result[label] = dict(player) if isinstance(player, dict) else dict(payload)
        return result

    def record_lap_state(
        self,
        lap_state: dict[str, Any],
        session_uid: int | None = None,
        packet_meta: dict[str, Any] | None = None,
    ) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            previous_packet = dict(self.active_lap_state)
            active_before = self.active_lap_number
            capture_before = self.capture_current_lap
            samples_before = len(self.active_samples)
            active_session = self.sessions.get(self.active_recording_id or "")
            status_before = active_session.status if active_session else None
            reasons: list[str] = []
            decision = "observe"
            flags: dict[str, Any] = {}
            lap_number = int(lap_state["lap_number"])
            if self.active_lap_number is None:
                packet_session_time = (packet_meta or {}).get("session_time")
                race_grid_start = bool(
                    active_session is not None
                    and active_session.mode == "race"
                    and active_session.status == "armed"
                    and lap_number == 1
                    and int(lap_state.get("last_lap_ms", 0)) == 0
                    and int(lap_state.get("current_lap_ms", 0)) <= 1_500
                    and isinstance(packet_session_time, (int, float))
                    and 0 <= float(packet_session_time) <= 5
                )
                self._begin_lap(lap_number, race_grid_start)
                flags = {"race_grid_start": race_grid_start}
                if race_grid_start:
                    active_session.status = "recording"
                    self._persist_session(active_session)
                    decision = "start_capture"
                    reasons.append("verified_race_grid_start")
                else:
                    # With no earlier Lap Data there is no proof that this is
                    # the beginning of a full lap. Stay armed until a crossing.
                    decision = "initial_observation"
                    reasons.append("no_previous_lap_packet_so_full_lap_start_is_unproven")
            else:
                old_number = self.active_lap_number
                old_time = self.active_lap_state.get("current_lap_ms")
                old_distance = self.active_lap_state.get("lap_distance_m")
                new_time = int(lap_state.get("current_lap_ms", 0))
                new_distance = float(lap_state.get("lap_distance_m", 0))
                track_length = float(self.track_length_m or 0)
                time_reversed = old_time is not None and new_time + 1_000 < int(old_time)
                distance_reversed = old_distance is not None and new_distance + 50 < float(old_distance)
                lap_number_reversed = lap_number < old_number
                near_line = new_time <= 5_000 and -200 <= new_distance <= max(250, track_length * 0.05)
                forward_number = lap_number > old_number
                old_total_distance = self.active_lap_state.get("total_distance_m")
                new_total_distance = lap_state.get("total_distance_m")
                total_distance_step = (
                    float(new_total_distance) - float(old_total_distance)
                    if old_total_distance is not None and new_total_distance is not None else None
                )
                total_distance_reversed = bool(
                    total_distance_step is not None and total_distance_step < -50
                )
                continuous_forward_distance = bool(
                    total_distance_step is not None
                    and -5 <= total_distance_step <= max(250, track_length * 0.05)
                )
                timed_same_number_wrap = bool(
                    old_time is not None and int(old_time) > 0
                    and new_time + 1_000 < int(old_time)
                )
                untimed_approach_wrap = bool(
                    old_time is not None and int(old_time) == 0
                    and 0 <= new_time <= 5_000
                )
                wrapped_same_number = bool(
                    lap_number == old_number
                    and track_length > 100
                    and old_distance is not None
                    and float(old_distance) >= track_length * 0.85
                    and new_distance <= track_length * 0.05
                    and continuous_forward_distance
                    and (timed_same_number_wrap or untimed_approach_wrap)
                )
                crossed_from_negative = bool(
                    lap_number == old_number
                    and old_distance is not None
                    and -200 <= float(old_distance) < 0 <= new_distance
                    and new_time <= 5_000
                )
                reset_to_start = bool(
                    lap_number <= old_number
                    and (time_reversed or distance_reversed or total_distance_reversed)
                    and old_distance is not None
                    and float(old_distance) > max(250, track_length * 0.05)
                    and not continuous_forward_distance
                    and new_time <= 5_000
                    and -200 <= new_distance <= 100
                )
                restart_lap_started = reset_to_start and new_distance >= 0
                restart_approach = bool(
                    lap_number <= old_number
                    and total_distance_reversed
                    and new_time <= 5_000
                )
                crossed_start_line = near_line and (forward_number or wrapped_same_number or crossed_from_negative)
                new_lap_started = crossed_start_line or restart_lap_started
                if self.diagnostics_enabled and (reset_to_start or restart_approach):
                    self._start_diagnostic_segment("restart-lap")
                elif self.diagnostics_enabled and (
                    lap_number_reversed
                    or ((time_reversed or distance_reversed or total_distance_reversed) and not new_lap_started)
                ):
                    self._start_diagnostic_segment(
                        "lap-number-rewind" if lap_number_reversed else "time-distance-rewind"
                    )
                completed_lap_available = crossed_start_line and int(lap_state.get("last_lap_ms", 0)) >= 30_000
                flags = {
                    "near_line": near_line, "forward_number": forward_number,
                    "wrapped_same_number": wrapped_same_number,
                    "timed_same_number_wrap": timed_same_number_wrap,
                    "untimed_approach_wrap": untimed_approach_wrap,
                    "total_distance_step_m": total_distance_step,
                    "total_distance_reversed": total_distance_reversed,
                    "continuous_forward_distance": continuous_forward_distance,
                    "crossed_from_negative": crossed_from_negative,
                    "reset_to_start": reset_to_start,
                    "restart_lap_started": restart_lap_started,
                    "restart_approach": restart_approach,
                    "new_lap_started": new_lap_started,
                    "completed_lap_available": completed_lap_available,
                }

                if completed_lap_available and self.capture_current_lap:
                    saved, finish_reason = self._finish_lap(int(lap_state["last_lap_ms"]))
                    decision = "finish_saved" if saved else "finish_rejected"
                    reasons.append(finish_reason)
                elif crossed_start_line and self.capture_current_lap:
                    reasons.append("crossing_has_no_official_completed_lap_time")
                if new_lap_started and self.active_recording_id is not None:
                    session = self.sessions[self.active_recording_id]
                    if session.status == "armed":
                        session.status = "recording"
                        self._persist_session(session)
                    capture = session.status == "recording"
                    start_kind = (
                        "restart_at_lap_start" if restart_lap_started
                        else "lap_number_advanced" if forward_number
                        else "same_number_wrap" if wrapped_same_number
                        else "negative_distance_crossing"
                    )
                    reasons.append(f"verified_start_{start_kind}")
                    if capture:
                        decision = f"{decision}_and_start_capture" if decision.startswith("finish_") else "start_capture"
                else:
                    capture = False
                    if forward_number and not near_line:
                        reasons.append("rejected_crossing_lap_number_advanced_but_time_or_distance_not_near_line")
                        decision = "rejected_crossing"
                    elif near_line and not (forward_number or wrapped_same_number or crossed_from_negative):
                        reasons.append("rejected_crossing_near_line_without_transition_evidence")
                if lap_number != old_number or new_lap_started:
                    if lap_number != old_number and not new_lap_started:
                        reasons.append("active_lap_changed_without_verified_crossing_capture_disabled")
                        if lap_number_reversed:
                            reasons.append("discard_lap_number_decreased_probable_restart_or_flashback")
                            decision = "discard_partial_lap"
                    self._begin_lap(lap_number, capture)
                    restart_event = {
                        "type": "restart_lap", "session_time": (packet_meta or {}).get("session_time"),
                        "frame_identifier": (packet_meta or {}).get("frame_identifier"),
                        "from_lap_time_ms": old_time, "to_lap_time_ms": new_time,
                        "from_lap_distance_m": old_distance, "to_lap_distance_m": new_distance,
                        "game_invalid": bool(lap_state.get("invalid", False)),
                    }
                    if restart_lap_started:
                        self.active_lap_events.append(restart_event)
                        self.pending_restart_event = None
                    elif reset_to_start:
                        self.pending_restart_event = restart_event
                    elif crossed_start_line and capture and self.pending_restart_event is not None:
                        self.active_lap_events.append(self.pending_restart_event)
                        self.pending_restart_event = None
                else:
                    if time_reversed or distance_reversed or total_distance_reversed:
                        if restart_approach:
                            prior_event = self.active_lap_events[-1] if self.active_lap_events else None
                            restart_event = {
                                "type": "restart_lap", "session_time": (packet_meta or {}).get("session_time"),
                                "frame_identifier": (packet_meta or {}).get("frame_identifier"),
                                "from_lap_time_ms": (
                                    prior_event.get("from_lap_time_ms")
                                    if prior_event and prior_event.get("type") == "flashback" else old_time
                                ),
                                "to_lap_time_ms": new_time,
                                "from_lap_distance_m": (
                                    prior_event.get("from_lap_distance_m")
                                    if prior_event and prior_event.get("type") == "flashback" else old_distance
                                ),
                                "to_lap_distance_m": new_distance,
                                "game_invalid": bool(lap_state.get("invalid", False)),
                            }
                            self.active_samples = []
                            self.active_lap_events = []
                            self.active_invalid = False
                            self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
                            self.capture_current_lap = False
                            self.pending_restart_event = restart_event
                            reasons.append("restart_lap_relocated_to_untimed_approach")
                            decision = "restart_approach_armed"
                        elif self.capture_current_lap and lap_number == old_number:
                            retained = [
                                sample for sample in self.active_samples
                                if (
                                    not isinstance(sample.get("current_lap_ms"), (int, float))
                                    or float(sample["current_lap_ms"]) <= new_time
                                ) and (
                                    not isinstance(sample.get("lap_distance_m"), (int, float))
                                    or float(sample["lap_distance_m"]) <= new_distance
                                )
                            ]
                            removed = len(self.active_samples) - len(retained)
                            self.active_samples = retained
                            # A flashback creates a new game timeline. Validity
                            # follows the current game flag on that timeline;
                            # the recorder never invents invalidity.
                            self.active_invalid = False
                            self.active_lap_events.append({
                                "type": "flashback", "session_time": (packet_meta or {}).get("session_time"),
                                "frame_identifier": (packet_meta or {}).get("frame_identifier"),
                                "from_lap_time_ms": old_time, "to_lap_time_ms": new_time,
                                "from_lap_distance_m": old_distance, "to_lap_distance_m": new_distance,
                                "pruned_sample_count": removed,
                                "game_invalid": bool(lap_state.get("invalid", False)),
                            })
                            reasons.append("flashback_pruned_superseded_samples_capture_continues")
                            decision = "flashback_continue_capture"
                        else:
                            self.active_samples = []
                            self.active_lap_events = []
                            self.active_invalid = False
                            self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
                            self.capture_current_lap = False
                            reasons.append(
                                "discard_time_and_distance_reversed" if time_reversed and distance_reversed
                                else "discard_time_reversed" if time_reversed else "discard_distance_reversed"
                            )
                            decision = "discard_partial_lap"
                    elif not reasons:
                        reasons.append("continue_current_lap")
                        decision = "continue_capture" if self.capture_current_lap else "continue_not_capturing"
            self.active_invalid = self.active_invalid or bool(lap_state["invalid"])
            self.active_lap_state.update(lap_state)
            self.latest.update(lap_state)
            self._touch()
            active_session = self.sessions.get(self.active_recording_id or "")
            if self.race_store is not None and decision not in {"observe", "continue_capture", "continue_not_capturing"}:
                self.race_store.append_event(
                    float((packet_meta or {}).get("session_time") or 0), decision,
                    {"reasons": reasons, "flags": flags, "packet": dict(lap_state)},
                    "recorder", lap_number,
                )
            self._write_diagnostic({
                "event": "lap_data_decision", "timestamp_utc": self._diagnostic_timestamp(),
                "session_uid": session_uid,
                "session_time": (packet_meta or {}).get("session_time"),
                "frame_identifier": (packet_meta or {}).get("frame_identifier"),
                "overall_frame_identifier": (packet_meta or {}).get("overall_frame_identifier"),
                "recording_session_id": self.active_recording_id,
                "packet": dict(lap_state), "track_length_m": self.track_length_m,
                "previous_packet": previous_packet,
                "before": {
                    "recording_status": status_before, "capture_current_lap": capture_before,
                    "active_lap_number": active_before, "sample_count": samples_before,
                },
                "after": {
                    "recording_status": active_session.status if active_session else None,
                    "capture_current_lap": self.capture_current_lap,
                    "active_lap_number": self.active_lap_number, "sample_count": len(self.active_samples),
                },
                "flags": flags, "decision": decision, "reasons": reasons,
            })

    def record_telemetry(self, telemetry: dict[str, Any], session_time: float, session_uid: int | None = None) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            sample = {
                "t": round(session_time, 3), "lap_distance_m": self.active_lap_state.get("lap_distance_m"),
                "current_lap_ms": self.active_lap_state.get("current_lap_ms"),
                "pit_status": self.active_lap_state.get("pit_status"), **telemetry,
            }
            motion = self.packet_state.get(PACKET_MOTION, {}).get("player")
            motion_ex = self.packet_state.get(PACKET_MOTION_EX)
            status = self.packet_state.get(PACKET_CAR_STATUS, {}).get("player")
            if isinstance(motion, dict):
                sample.update({key: motion.get(key) for key in ("world_x", "world_y", "world_z", "yaw", "g_lateral", "g_longitudinal")})
            if isinstance(motion_ex, dict):
                sample.update({key: motion_ex.get(key) for key in ("wheel_slip_ratio", "wheel_slip_angle", "front_wheels_angle", "chassis_yaw")})
                slip_ratio = motion_ex.get("wheel_slip_ratio")
                slip_angle = motion_ex.get("wheel_slip_angle")
                if isinstance(slip_ratio, list) and len(slip_ratio) == 4:
                    sample["front_wheel_slip_ratio"] = sum(abs(float(value)) for value in slip_ratio[2:4]) / 2
                    sample["rear_wheel_slip_ratio"] = sum(abs(float(value)) for value in slip_ratio[0:2]) / 2
                if isinstance(slip_angle, list) and len(slip_angle) == 4:
                    sample["front_wheel_slip_angle"] = sum(abs(float(value)) for value in slip_angle[2:4]) / 2
                    sample["rear_wheel_slip_angle"] = sum(abs(float(value)) for value in slip_angle[0:2]) / 2
            if isinstance(status, dict):
                sample.update({key: status.get(key) for key in (
                    "ers_store_j", "ers_deployed_this_lap_j", "ers_harvested_mguk_j", "ers_harvested_mguh_j",
                )})
            if self.capture_current_lap and self.active_lap_number is not None:
                self.active_samples.append(sample)
            self.latest.update(sample)
            self._touch()

    def record_full_packet(self, header: Any, payload: dict[str, Any]) -> None:
        """Persist and correlate a decoded packet without changing lap-boundary logic."""
        with self.lock:
            self._observe_uid(int(header.session_uid))
            previous = self.packet_state.get(int(header.packet_id))
            self.packet_state[int(header.packet_id)] = payload
            if self.capture_current_lap and int(self.active_lap_state.get("current_lap_ms", 9_999_999)) <= 1_500:
                self.active_lap_context = self._current_race_context()
            player = payload.get("player") if isinstance(payload, dict) else None
            distance = player.get("lap_distance_m") if isinstance(player, dict) else self.active_lap_state.get("lap_distance_m")
            if self.race_store is None:
                return
            self.race_store.append_packet(header, payload, distance if isinstance(distance, (int, float)) else None)
            lap_number = int(self.active_lap_state.get("lap_number", 0)) or None
            if header.packet_id == PACKET_EVENT:
                code = str(payload.get("code", "event"))
                event_names = {
                    "SSTA": "session_started", "SEND": "session_ended", "FTLP": "fastest_lap",
                    "RTMT": "retirement", "DRSE": "drs_enabled", "DRSD": "drs_disabled",
                    "TMPT": "team_mate_in_pits", "CHQF": "chequered_flag", "RCWN": "race_winner",
                    "PENA": "penalty", "SPTP": "speed_trap", "STLG": "start_lights",
                    "LGOT": "lights_out", "DTSV": "drive_through_served", "SGSV": "stop_go_served",
                    "FLBK": "flashback", "RDFL": "red_flag", "OVTK": "overtake",
                    "SCAR": "safety_car", "COLL": "collision", "BUTN": "controller_input",
                }
                event_details = dict(payload)
                event_details["_packet"] = {
                    "frame_identifier": int(header.frame_identifier),
                    "overall_frame_identifier": int(header.overall_frame_identifier),
                    "player_car_index": int(header.player_car_index),
                    "session_uid": int(header.session_uid),
                }
                self.race_store.append_event(header.session_time, event_names.get(code, code.lower()), event_details, "udp_event", lap_number)
            elif header.packet_id == PACKET_LAP_DATA and isinstance(player, dict):
                old_player = previous.get("player") if isinstance(previous, dict) else None
                if isinstance(old_player, dict):
                    for key, event_type in (("pit_status", "pit_status_change"), ("position", "position_change")):
                        if old_player.get(key) != player.get(key):
                            if key == "pit_status":
                                event_type = "pit_exit" if player.get(key) == 0 else "pit_entry" if old_player.get(key) == 0 else "pit_lane_state_change"
                            self.race_store.append_event(header.session_time, event_type, {"from": old_player.get(key), "to": player.get(key)}, "inferred", player.get("lap_number"))
                    if not old_player.get("pit_stop_time_ms") and player.get("pit_stop_time_ms"):
                        self.race_store.append_event(header.session_time, "pit_stop", {"duration_ms": player.get("pit_stop_time_ms")}, "inferred", player.get("lap_number"))
            elif header.packet_id == PACKET_CAR_STATUS and isinstance(player, dict):
                old_player = previous.get("player") if isinstance(previous, dict) else None
                if isinstance(old_player, dict) and old_player.get("actual_tyre_compound") != player.get("actual_tyre_compound"):
                    self.race_store.append_event(header.session_time, "compound_change", {"from": old_player.get("actual_tyre_compound"), "to": player.get("actual_tyre_compound")}, "inferred", lap_number)
            elif header.packet_id == PACKET_FINAL_CLASSIFICATION:
                self.race_store.append_event(header.session_time, "final_classification", payload, "udp_event", lap_number)
            elif header.packet_id == PACKET_SESSION and isinstance(previous, dict):
                for key, event_type in (("safety_car_status", "safety_car_state_change"), ("weather", "weather_change")):
                    if previous.get(key) != payload.get(key):
                        self.race_store.append_event(header.session_time, event_type, {"from": previous.get(key), "to": payload.get(key)}, "inferred", lap_number)
            elif header.packet_id == PACKET_CAR_DAMAGE and isinstance(player, dict):
                old_player = previous.get("player") if isinstance(previous, dict) else None
                if isinstance(old_player, dict) and old_player != player:
                    # Wear is a continuous engineering channel, not a collision
                    # or damage event. Keep it in packet/lap data without
                    # flooding the race-event timeline every few seconds.
                    noisy = {"car_index", "tyre_wear_pct", "tyre_damage_pct"}
                    changed = {key: {"from": old_player.get(key), "to": value} for key, value in player.items() if key not in noisy and old_player.get(key) != value}
                    if changed:
                        self.race_store.append_event(header.session_time, "damage_change", changed, "inferred", lap_number)

    def _finish_lap(self, time_ms: int) -> tuple[bool, str]:
        if self.active_recording_id is None:
            return False, "finish_rejected_no_active_recording"
        if time_ms < 30_000:
            return False, "finish_rejected_official_time_below_30000_ms"
        if len(self.active_samples) < 30:
            return False, "finish_rejected_fewer_than_30_samples"
        session = self.sessions[self.active_recording_id]
        if session.game_session_uid != self.game_session_uid:
            return False, "finish_rejected_game_session_uid_mismatch"
        state = self.active_lap_state
        saved_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        lap = enrich_lap(Lap(
            number=self.active_lap_number or 0, time_ms=time_ms, invalid=self.active_invalid,
            samples=self.active_samples, events=self.active_lap_events,
            setup=self.active_lap_setup, saved_at=saved_at,
            sector1_ms=_positive(state.get("sector1_ms")), sector2_ms=_positive(state.get("sector2_ms")),
            recording_session_id=session.id, game_session_uid=session.game_session_uid,
            mode=session.mode, track_id=session.track_id, track_length_m=session.track_length_m,
            race_context={"start": self.active_lap_context or {}, "finish": self._current_race_context()},
        ))
        self.session_laps[session.id].append(lap)
        session.lap_ids.append(lap.id)
        self._refresh_lap_index()
        payload = asdict(lap)
        payload["sample_count"] = len(lap.samples)
        directory = self.sessions_dir / session.id
        self._atomic_json(directory / f"{lap.id}.json", payload, compact=True)
        self._persist_session(session)
        self.pb_registry.consider(lap, session.name, self.notes.get(lap.id, ""))
        return True, "finish_saved_official_time_and_minimum_samples_present"

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            real = [session for session in self.sessions.values() if session.id != LEGACY_SESSION_ID]
            real.sort(key=lambda item: item.started_at, reverse=True)
            ordered = real + [self.sessions[LEGACY_SESSION_ID]]
            return [self._session_summary(session) for session in ordered]

    def _session_summary(self, session: RecordingSession) -> dict[str, Any]:
        result = session.summary(self.session_laps[session.id])
        result["can_delete"] = (
            session.id != LEGACY_SESSION_ID
            and session.id != self.active_recording_id
            and session.status not in OPEN_STATUSES
        )
        if session.id == self.active_recording_id and self.race_store is not None:
            result["continuous_store"] = self.race_store.summary()
        elif session.race_store_file:
            path = self.sessions_dir / session.id / session.race_store_file
            if path.exists():
                # The library endpoint is polled frequently. Do not scan every
                # historical SQLite packet table just to draw session cards;
                # detailed continuity is calculated on explicit report export.
                result["continuous_store"] = {
                    "path": session.race_store_file, "bytes": path.stat().st_size,
                    "details_deferred": True,
                }
        return result

    def list_profiles(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            session = self.sessions.get(session_id or self.selected_session_id or "")
            return self.profile_store.list(2025, session.track_id) if session and session.track_id is not None else self.profile_store.list(2025)

    def save_profile(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            return self.profile_store.save(payload)

    def delete_profile(self, profile_id: str, confirmed: bool = False) -> dict[str, Any]:
        with self.lock:
            if not confirmed:
                raise CircuitProfileError("Circuit profile deletion must be explicitly confirmed")
            result = self.profile_store.delete(profile_id)
            affected = []
            for session in self.sessions.values():
                if session.circuit_profile_id == profile_id:
                    session.circuit_profile_id = None
                    self._persist_session(session)
                    affected.append(session.id)
            result["cleared_session_ids"] = affected
            return result

    def select_profile(self, session_id: str, profile_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            profile = self.profile_store.get(profile_id)
            if session is None:
                raise SessionStateError("Unknown session ID")
            if profile is None:
                raise SessionStateError("Unknown circuit profile")
            if profile.get("packet_format") != session.packet_format or profile.get("track_id") != session.track_id:
                raise SessionStateError("Circuit profile is incompatible with this session's format or track ID")
            session.circuit_profile_id = profile_id
            self._persist_session(session)
            return self._session_summary(session)

    def race_timeline(self, session_id: str, lap_from: int | None = None, lap_to: int | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return self._race_timeline_unlocked(session_id, lap_from, lap_to)

    def _race_timeline_unlocked(self, session_id: str, lap_from: int | None, lap_to: int | None) -> list[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None or not session.race_store_file:
            raise SessionStateError("Continuous race data is unavailable for this session")
        if session_id == self.active_recording_id and self.race_store is not None:
            return self.race_store.timeline(lap_from, lap_to)
        path = self.sessions_dir / session_id / session.race_store_file
        if not path.exists():
            raise SessionStateError("Continuous race data is unavailable for this legacy session")
        return RaceSessionStore.read_timeline(path, lap_from, lap_to)

    def _race_packet_export_unlocked(self, session: RecordingSession, start_time: float, end_time: float) -> bytes | None:
        if not session.race_store_file:
            return None
        if session.id == self.active_recording_id and self.race_store is not None:
            return self.race_store.export_packets(start_time, end_time)
        path = self.sessions_dir / session.id / session.race_store_file
        if not path.exists():
            return None
        return RaceSessionStore.read_packet_export(path, start_time, end_time)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            latest = dict(self.latest)
            latest["connected"] = self.last_packet_at is not None and monotonic() - self.last_packet_at < 3.0
            active = self.sessions.get(self.active_recording_id or "")
            selected = self.sessions.get(self.selected_session_id or "")
            recording = self._session_summary(active) if active else None
            if recording is not None:
                recording["capturing_lap_number"] = self.active_lap_number if self.capture_current_lap else None
            return {
                "latest": latest,
                "laps": [lap.summary(self.notes.get(lap.id, "")) for lap in self.session_laps.get(self.selected_session_id or "", [])],
                "recording": recording,
                "selected_session_id": self.selected_session_id,
                "selected_is_active": bool(active and selected and active.id == selected.id),
                "diagnostics": self.diagnostic_status(),
            }

    def list_laps(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            selected = session_id or self.selected_session_id or LEGACY_SESSION_ID
            if selected not in self.sessions:
                raise SessionStateError("Unknown session ID")
            return [lap.summary(self.notes.get(lap.id, "")) for lap in self.session_laps[selected]]

    def get_lap_object(self, lap_id: str) -> Lap | None:
        if lap_id.startswith("pb:"):
            entry = self.pb_registry.get(lap_id[3:])
            if entry is not None:
                try:
                    return self._lap_from_payload(entry["lap"])
                except (TypeError, KeyError, ValueError):
                    return None
        return next((lap for lap in self.laps if lap.id == lap_id), None)

    def lap(self, lap_id: str) -> dict[str, Any] | None:
        with self.lock:
            lap = self.get_lap_object(lap_id)
            if lap:
                return asdict(lap) | {
                    "id": lap.id, "sample_count": len(lap.samples), "note": self.notes.get(lap.id, ""),
                    "setup_fields": setup_rows(lap.setup),
                }
        return None

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any], compact: bool = False) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")) if compact else json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def amend_lap_setup(self, lap_id: str, values: Any) -> dict[str, Any]:
        """Amend only setup metadata, retaining exact pre-change lap/PB backups."""
        clean = validate_manual_values(values)
        with self.lock:
            lap = next((item for item in self.laps if item.id == lap_id), None)
            if lap is None:
                raise SessionStateError("Lap not found")
            lap_path = self.capture_dir / f"{lap.id}.json" if lap.recording_session_id == LEGACY_SESSION_ID else self.sessions_dir / lap.recording_session_id / f"{lap.id}.json"
            try:
                original_lap = lap_path.read_bytes()
                lap_payload = json.loads(original_lap)
            except (OSError, ValueError, TypeError) as error:
                raise SessionStateError("Original lap JSON is unavailable or invalid") from error

            current = with_provenance(lap.setup, "legacy_decoded_udp") or {"_provenance": {}}
            changed = {key: value for key, value in clean.items() if current.get(key) != value}
            if not changed:
                raise SetupValidationError("No setup values changed")

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            audit_dir = self.capture_dir / "audit" / "setup-amendments" / lap.id / stamp
            audit_dir.mkdir(parents=True, exist_ok=False)
            (audit_dir / "original-lap.json").write_bytes(original_lap)

            pb_entry = next((entry for entry in self.pb_registry.entries.values() if entry.get("source_lap_id") == lap.id), None)
            pb_path = None
            if pb_entry is not None:
                pb_path = self.pb_registry.path_for(str(pb_entry["key"]))
                try:
                    (audit_dir / "original-personal-best.json").write_bytes(pb_path.read_bytes())
                except OSError as error:
                    raise SessionStateError("PB record exists but cannot be backed up; no files were changed") from error

            provenance = dict(current.get("_provenance") or {})
            current.update(changed)
            provenance.update({key: "manual" for key in changed})
            current["_provenance"] = provenance
            current["_amended_at"] = utc_now()
            current["_audit_record"] = str(audit_dir.relative_to(self.capture_dir))
            manifest = {
                "lap_id": lap.id, "source_session_id": lap.recording_session_id,
                "created_at": current["_amended_at"], "changed_fields": changed,
                "preserved_fields": {key: value for key, value in (lap.setup or {}).items() if not key.startswith("_") and key not in changed},
                "pb_key": pb_entry.get("key") if pb_entry else None,
            }
            (audit_dir / "amendment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            lap_payload["setup"] = current
            self._atomic_json(lap_path, lap_payload, compact=True)
            lap.setup = current
            pb_synced = False
            if pb_entry is not None:
                self.pb_registry.update_source_setup(lap.id, current)
                pb_synced = True
            return {
                "lap": lap.summary(self.notes.get(lap.id, "")), "setup_fields": setup_rows(current),
                "changed_fields": list(changed), "pb_synced": pb_synced,
                "pb_message": "Personal best snapshot synchronized" if pb_synced else "No PB snapshot found; the corrected lap will sync when the PB registry is built",
                "audit_record": str(audit_dir.relative_to(self.capture_dir)),
            }

    def set_note(self, lap_id: str, note: str) -> dict[str, Any] | None:
        with self.lock:
            lap = self.get_lap_object(lap_id)
            if lap is None:
                return None
            clean_note = note.strip()[:240]
            if clean_note:
                self.notes[lap_id] = clean_note
            else:
                self.notes.pop(lap_id, None)
            self.notes_path.write_text(json.dumps(self.notes, indent=2, sort_keys=True), encoding="utf-8")
            self.pb_registry.update_source_note(lap_id, clean_note)
            return {"id": lap_id, "note": clean_note}

    def list_personal_bests(self) -> list[dict[str, Any]]:
        with self.lock:
            return [entry | {"track_name": track_name(entry.get("track_id"))} for entry in self.pb_registry.summaries()]

    def personal_best(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return self.pb_registry.get(key)

    def rebuild_personal_bests(self) -> dict[str, int]:
        with self.lock:
            return self.pb_registry.rebuild(
                self.laps,
                lambda session_id: self.sessions.get(session_id, self.sessions[LEGACY_SESSION_ID]).name,
                lambda lap_id: self.notes.get(lap_id, ""),
            )

    def _raw_lap(self, lap: Lap) -> bytes | None:
        if lap.recording_session_id == LEGACY_SESSION_ID:
            path = self.capture_dir / f"{lap.id}.json"
        else:
            path = self.sessions_dir / lap.recording_session_id / f"{lap.id}.json"
        try:
            return path.read_bytes()
        except OSError:
            return None

    def export_session(
        self, session_id: str, scope: str, lap_ids: list[str], export_format: str,
        report_type: str = "auto",
    ) -> tuple[str, str, bytes]:
        from .reports import ExportError, build_markdown, build_zip, safe_filename

        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise ExportError("Unknown session ID")
            available = self.session_laps[session_id]
            if scope == "session":
                included = list(available)
            elif scope == "selected":
                requested = list(dict.fromkeys(lap_ids))
                if not requested:
                    raise ExportError("Select at least one lap before exporting")
                lookup = {lap.id: lap for lap in available}
                unknown = [lap_id for lap_id in requested if lap_id not in lookup]
                if unknown:
                    raise ExportError(f"Lap does not belong to this session: {unknown[0]}")
                included = [lookup[lap_id] for lap_id in requested]
            else:
                raise ExportError("Export scope must be 'session' or 'selected'")
            continuous_path = self.sessions_dir / session.id / session.race_store_file if session.race_store_file else None
            if not included and not (scope == "session" and continuous_path and continuous_path.exists()):
                raise ExportError("This export contains no completed laps")
            timeline: list[dict[str, Any]] | None = None
            if session.race_store_file:
                try:
                    lap_numbers = [lap.number for lap in included]
                    timeline = self._race_timeline_unlocked(
                        session_id, min(lap_numbers) if lap_numbers else None, max(lap_numbers) if lap_numbers else None,
                    )
                except SessionStateError:
                    timeline = None
            if session.id == self.active_recording_id and self.race_store is not None:
                storage_summary = self.race_store.summary()
            elif continuous_path and continuous_path.exists():
                try:
                    storage_summary = RaceSessionStore.read_summary(continuous_path)
                except sqlite3.Error:
                    storage_summary = {"path": session.race_store_file, "error": "unreadable"}
            else:
                storage_summary = None
            circuit_profile = self.profile_store.get(session.circuit_profile_id) if session.circuit_profile_id else None
            report = build_markdown(
                session, included, self.notes, track_name(session.track_id, session.track_name_override),
                timeline, storage_summary, circuit_profile, report_type,
            )
            resolved_type = report_type if report_type != "auto" else ("race" if session.mode == "race" else "time_trial" if session.mode == "time_trial" else "lap_analysis")
            base = safe_filename(f"{session.name}-{resolved_type.replace('_', '-')}")
            if export_format == "markdown":
                return f"{base}.md", "text/markdown; charset=utf-8", report.encode("utf-8")
            if export_format == "zip":
                raw = {lap.id: self._raw_lap(lap) for lap in included}
                if any(value is None for value in raw.values()):
                    missing = next(key for key, value in raw.items() if value is None)
                    raise ExportError(f"Original JSON is unavailable for lap {missing}")
                continuous_data = None
                scoped_packets = None
                if scope == "session" and session.race_store_file:
                    path = self.sessions_dir / session.id / session.race_store_file
                    if session.id == self.active_recording_id and self.race_store is not None:
                        continuous_data = self.race_store.snapshot_bytes()
                    elif path.exists():
                        continuous_data = RaceSessionStore.read_snapshot(path)
                elif scope == "selected" and session.race_store_file:
                    starts = [lap.first_session_time for lap in included if lap.first_session_time is not None]
                    ends = [lap.last_session_time for lap in included if lap.last_session_time is not None]
                    if starts and ends:
                        # Include a short boundary margin for slower state/event
                        # packets immediately before and after the lap samples.
                        scoped_packets = self._race_packet_export_unlocked(
                            session, max(0.0, min(starts) - 1.0), max(ends) + 1.0,
                        )
                return f"{base}.zip", "application/zip", build_zip(
                    report, session, included, raw, timeline, continuous_data, scoped_packets, circuit_profile,
                )  # type: ignore[arg-type]
            raise ExportError("Export format must be 'markdown' or 'zip'")

    def export_lap_analysis(self, baseline_id: str, candidate_id: str) -> tuple[str, str, bytes]:
        from .reports import build_lap_analysis_markdown, safe_filename

        with self.lock:
            baseline, candidate = self.get_lap_object(baseline_id), self.get_lap_object(candidate_id)
            if baseline is None or candidate is None:
                missing = baseline_id if baseline is None else candidate_id
                raise ExportError(f"Unknown lap ID: {missing}")
            session = self.sessions.get(baseline.recording_session_id)
            candidate_session = self.sessions.get(candidate.recording_session_id)
            profile = self.profile_store.get(session.circuit_profile_id) if session and session.circuit_profile_id else None
            if profile is None and candidate_session and candidate_session.circuit_profile_id:
                profile = self.profile_store.get(candidate_session.circuit_profile_id)
            if (
                baseline.mode == candidate.mode == "race"
                and baseline.recording_session_id != candidate.recording_session_id
                and (not session or not candidate_session or session.circuit_profile_id != candidate_session.circuit_profile_id)
            ):
                raise ExportError("Cross-session race laps must select the same verified circuit profile")
            report = build_lap_analysis_markdown(
                baseline, candidate, self.notes.get(baseline.id, ""), self.notes.get(candidate.id, ""),
                track_name(baseline.track_id), profile,
            )
            filename = safe_filename(f"{track_name(baseline.track_id)}-lap-{baseline.number}-vs-{candidate.number}-analysis")
            return f"{filename}.md", "text/markdown; charset=utf-8", report.encode("utf-8")


class UdpReceiver(Thread):
    def __init__(self, store: SessionStore, port: int = 20777) -> None:
        super().__init__(daemon=True, name="f1-udp-receiver")
        self.store = store
        self.port = port
        self.stop_event = Event()

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(0.5)
            while not self.stop_event.is_set():
                try:
                    data, _address = sock.recvfrom(8192)
                except TimeoutError:
                    continue
                self._handle_safely(data)

    def _handle_safely(self, data: bytes) -> bool:
        try:
            self._handle(data)
            return True
        except Exception as error:
            # A malformed packet or persistence failure must not stop every
            # subsequent lap from being received.
            self.store.record_receiver_error(error)
            return False

    def _handle(self, data: bytes) -> None:
        header = decode_header(data)
        if header is None or not supported_header(header):
            return
        decoded: dict[str, Any] | None = None
        if header.packet_id == PACKET_SESSION:
            session = decode_session(data, header)
            if session:
                self.store.record_game_session(session)
        elif header.packet_id == PACKET_LAP_DATA:
            lap_state = decode_player_lap_data(data, header)
            if lap_state:
                self.store.record_lap_state(lap_state, header.session_uid, {
                    "session_time": round(header.session_time, 6),
                    "frame_identifier": header.frame_identifier,
                    "overall_frame_identifier": header.overall_frame_identifier,
                })
        elif header.packet_id == PACKET_CAR_TELEMETRY:
            telemetry = decode_player_car_telemetry(data, header)
            if telemetry:
                self.store.record_telemetry(telemetry, header.session_time, header.session_uid)
        elif header.packet_id == PACKET_CAR_SETUPS:
            setup = decode_player_setup(data, header)
            if setup:
                self.store.record_setup(setup, header.session_uid, {
                    "session_time": round(float(header.session_time), 6),
                    "frame_identifier": int(header.frame_identifier),
                    "overall_frame_identifier": int(header.overall_frame_identifier),
                })
        decoded = decode_packet(data, header)
        if decoded is not None:
            self.store.record_full_packet(header, decoded)
        if header.packet_id == PACKET_EVENT and decode_event_code(data, header) == "SEND":
            self.store.game_session_ended(header.session_uid)

    def stop(self) -> None:
        self.stop_event.set()
