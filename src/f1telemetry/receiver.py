"""UDP receiver, persistent recording sessions, and local lap storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from .circuits import is_known_track, track_name
from .protocol import (
    PACKET_CAR_SETUPS, PACKET_CAR_TELEMETRY, PACKET_EVENT, PACKET_LAP_DATA,
    PACKET_SESSION, decode_event_code, decode_header, decode_player_car_telemetry,
    decode_player_lap_data, decode_player_setup, decode_session,
)
from .personal_bests import PersonalBestRegistry
from .quality import lap_quality
from .setups import SetupValidationError, setup_rows, validate_manual_values, with_provenance

LEGACY_SESSION_ID = "legacy"
OPEN_STATUSES = {"armed", "recording"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Lap:
    number: int
    time_ms: int
    invalid: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
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

    @property
    def id(self) -> str:
        return f"lap-{self.saved_at}-{self.number}"

    def summary(self, note: str = "") -> dict[str, Any]:
        return {
            "id": self.id, "number": self.number, "time_ms": self.time_ms,
            "invalid": self.invalid, "sample_count": len(self.samples),
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
        self.pb_registry = PersonalBestRegistry(self.capture_dir / "personal_bests")
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
        self.active_recording_id: str | None = None
        self.selected_session_id: str | None = None
        self.manual_stop_uid: int | None = None
        self.current_setup: dict[str, Any] | None = None
        self.active_lap_number: int | None = None
        self.active_invalid = False
        self.active_samples: list[dict[str, Any]] = []
        self.active_lap_state: dict[str, Any] = {}
        self.active_lap_setup: dict[str, Any] | None = None
        self.capture_current_lap = False
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

    def _observe_uid(self, session_uid: int | None) -> None:
        if session_uid is None:
            return
        if self.game_session_uid is None:
            self.game_session_uid = session_uid
            return
        if session_uid == self.game_session_uid:
            return
        self._close_active("completed")
        self._reset_lap()
        self.game_session_uid = session_uid
        self.game_mode, self.game_session_type = "unknown", None
        self.track_id, self.track_length_m = None, None
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
            self.latest.update({
                "game_session_uid": self.game_session_uid, "game_mode": self.game_mode,
                "session_type": self.game_session_type, "track_id": self.track_id,
                "track_length_m": self.track_length_m, "track_name": track_name(self.track_id),
            })
            self._touch()
            if self.game_mode == "race" and self.active_recording_id is None and self.manual_stop_uid != self.game_session_uid:
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
        )
        self.sessions[session.id], self.session_laps[session.id] = session, []
        self.active_recording_id = session.id
        if not automatic:
            self.selected_session_id = session.id
        elif self.selected_session_id is None:
            self.selected_session_id = session.id
        self.capture_current_lap = at_start
        self.active_samples = []
        if at_start and self.active_lap_number is not None:
            self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
            self.active_invalid = bool(self.active_lap_state.get("invalid", False))
        self._persist_session(session)
        return session

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
        self._persist_session(session)
        self.active_recording_id = None
        self.capture_current_lap = False
        self.active_samples = []

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

    def record_setup(self, setup: dict[str, Any], session_uid: int | None = None) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            decoded = with_provenance(setup, "decoded_udp") or {}
            self.current_setup = decoded
            if (
                self.capture_current_lap
                and self.active_lap_setup is None
                and float(self.active_lap_state.get("current_lap_ms", 0)) <= 1_500
            ):
                self.active_lap_setup = dict(decoded)
            self.latest.update(setup)
            self.latest["setup"] = dict(decoded)
            self._touch()

    def _reset_lap(self) -> None:
        self.active_lap_number = None
        self.active_invalid = False
        self.active_samples = []
        self.active_lap_state = {}
        self.active_lap_setup = None
        self.capture_current_lap = False

    def _begin_lap(self, lap_number: int, capture: bool) -> None:
        self.active_lap_number = lap_number
        self.active_invalid = False
        self.active_samples = []
        self.active_lap_state = {}
        self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
        self.capture_current_lap = capture

    def record_lap_state(self, lap_state: dict[str, Any], session_uid: int | None = None) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            lap_number = int(lap_state["lap_number"])
            if self.active_lap_number is None:
                # With no earlier Lap Data there is no proof that this is the
                # beginning of a full lap. Stay armed until a verified crossing.
                self._begin_lap(lap_number, False)
            else:
                old_number = self.active_lap_number
                old_time = self.active_lap_state.get("current_lap_ms")
                old_distance = self.active_lap_state.get("lap_distance_m")
                new_time = int(lap_state.get("current_lap_ms", 0))
                new_distance = float(lap_state.get("lap_distance_m", 0))
                track_length = float(self.track_length_m or 0)
                near_line = new_time <= 5_000 and -200 <= new_distance <= max(250, track_length * 0.05)
                forward_number = lap_number > old_number
                wrapped_same_number = bool(
                    lap_number == old_number
                    and track_length > 100
                    and old_distance is not None
                    and float(old_distance) >= track_length * 0.85
                    and new_distance <= track_length * 0.05
                    and old_time is not None
                    and new_time + 1_000 < int(old_time)
                )
                crossed_from_negative = bool(
                    lap_number == old_number
                    and old_distance is not None
                    and -200 <= float(old_distance) < 0 <= new_distance
                    and new_time <= 5_000
                )
                new_lap_started = near_line and (forward_number or wrapped_same_number or crossed_from_negative)
                completed_lap_available = new_lap_started and int(lap_state.get("last_lap_ms", 0)) >= 30_000

                if completed_lap_available and self.capture_current_lap:
                    self._finish_lap(int(lap_state["last_lap_ms"]))
                if new_lap_started and self.active_recording_id is not None:
                    session = self.sessions[self.active_recording_id]
                    if session.status == "armed":
                        session.status = "recording"
                        self._persist_session(session)
                    capture = session.status == "recording"
                else:
                    capture = False
                if lap_number != old_number or new_lap_started:
                    self._begin_lap(lap_number, capture)
                else:
                    time_reversed = old_time is not None and new_time + 1_000 < int(old_time)
                    distance_reversed = old_distance is not None and new_distance + 50 < float(old_distance)
                    if time_reversed or distance_reversed:
                        self.active_samples = []
                        self.active_invalid = False
                        self.active_lap_setup = dict(self.current_setup) if self.current_setup else None
                        self.capture_current_lap = False
            self.active_invalid = self.active_invalid or bool(lap_state["invalid"])
            self.active_lap_state.update(lap_state)
            self.latest.update(lap_state)
            self._touch()

    def record_telemetry(self, telemetry: dict[str, Any], session_time: float, session_uid: int | None = None) -> None:
        with self.lock:
            self._observe_uid(session_uid)
            sample = {
                "t": round(session_time, 3), "lap_distance_m": self.active_lap_state.get("lap_distance_m"),
                "current_lap_ms": self.active_lap_state.get("current_lap_ms"), **telemetry,
            }
            if self.capture_current_lap and self.active_lap_number is not None:
                self.active_samples.append(sample)
            self.latest.update(sample)
            self._touch()

    def _finish_lap(self, time_ms: int) -> None:
        if self.active_recording_id is None or time_ms < 30_000 or len(self.active_samples) < 30:
            return
        session = self.sessions[self.active_recording_id]
        if session.game_session_uid != self.game_session_uid:
            return
        state = self.active_lap_state
        saved_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        lap = enrich_lap(Lap(
            number=self.active_lap_number or 0, time_ms=time_ms, invalid=self.active_invalid,
            samples=self.active_samples, setup=self.active_lap_setup, saved_at=saved_at,
            sector1_ms=_positive(state.get("sector1_ms")), sector2_ms=_positive(state.get("sector2_ms")),
            recording_session_id=session.id, game_session_uid=session.game_session_uid,
            mode=session.mode, track_id=session.track_id, track_length_m=session.track_length_m,
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

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            real = [session for session in self.sessions.values() if session.id != LEGACY_SESSION_ID]
            real.sort(key=lambda item: item.started_at, reverse=True)
            ordered = real + [self.sessions[LEGACY_SESSION_ID]]
            return [session.summary(self.session_laps[session.id]) for session in ordered]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            latest = dict(self.latest)
            latest["connected"] = self.last_packet_at is not None and monotonic() - self.last_packet_at < 3.0
            active = self.sessions.get(self.active_recording_id or "")
            selected = self.sessions.get(self.selected_session_id or "")
            return {
                "latest": latest,
                "laps": [lap.summary(self.notes.get(lap.id, "")) for lap in self.session_laps.get(self.selected_session_id or "", [])],
                "recording": active.summary(self.session_laps[active.id]) if active else None,
                "selected_session_id": self.selected_session_id,
                "selected_is_active": bool(active and selected and active.id == selected.id),
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

    def export_session(self, session_id: str, scope: str, lap_ids: list[str], export_format: str) -> tuple[str, str, bytes]:
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
            if not included:
                raise ExportError("This export contains no completed laps")
            report = build_markdown(session, included, self.notes, track_name(session.track_id, session.track_name_override))
            base = safe_filename(session.name)
            if export_format == "markdown":
                return f"{base}.md", "text/markdown; charset=utf-8", report.encode("utf-8")
            if export_format == "zip":
                raw = {lap.id: self._raw_lap(lap) for lap in included}
                if any(value is None for value in raw.values()):
                    missing = next(key for key, value in raw.items() if value is None)
                    raise ExportError(f"Original JSON is unavailable for lap {missing}")
                return f"{base}.zip", "application/zip", build_zip(report, session, included, raw)  # type: ignore[arg-type]
            raise ExportError("Export format must be 'markdown' or 'zip'")


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
                self._handle(data)

    def _handle(self, data: bytes) -> None:
        header = decode_header(data)
        if header is None or header.packet_format != 2025 or header.game_year != 25:
            return
        if header.packet_id == PACKET_SESSION:
            session = decode_session(data, header)
            if session:
                self.store.record_game_session(session)
        elif header.packet_id == PACKET_LAP_DATA:
            lap_state = decode_player_lap_data(data, header)
            if lap_state:
                self.store.record_lap_state(lap_state, header.session_uid)
        elif header.packet_id == PACKET_CAR_TELEMETRY:
            telemetry = decode_player_car_telemetry(data, header)
            if telemetry:
                self.store.record_telemetry(telemetry, header.session_time, header.session_uid)
        elif header.packet_id == PACKET_CAR_SETUPS:
            setup = decode_player_setup(data, header)
            if setup:
                self.store.record_setup(setup, header.session_uid)
        elif header.packet_id == PACKET_EVENT and decode_event_code(data, header) == "SEND":
            self.store.game_session_ended(header.session_uid)

    def stop(self) -> None:
        self.stop_event.set()
