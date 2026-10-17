"""UDP receiver, persistent recording sessions, and local lap storage."""

from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
import socket
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from .circuits import is_known_track, track_name
from .circuit_profiles import CircuitProfileStore, profile_identity
from .application.administration import RuntimeSettingsService, SessionTrashService
from .application.amendments import SetupAmendmentService
from .application.archive import OPEN_STATUSES, SessionArchiveRepository
from .application.comparisons import (
    ComparisonPolicyError,
    resolve_comparison_profile,
)
from .application.diagnostics import (
    MAX_DIAGNOSTIC_EVENTS,
    MAX_DIAGNOSTIC_SEGMENTS,
    DiagnosticCapture,
)
from .application.profiles import CircuitProfileService
from .application.recorder import RecorderApplicationService
from .application.exports import (
    CapturedComparisonExport,
    capture_session_export,
    render_comparison_export,
    render_session_export,
)
from .domain.errors import SessionStateError
from .domain.models import LEGACY_SESSION_ID, Lap, RecordingSession, enrich_lap
from .domain.recording import (
    DecisionCode,
    LapDataObservation,
    RecorderState,
    RecordingContext,
    decide_lap_boundary,
)
from .protocol import (
    PACKET_CAR_DAMAGE, PACKET_CAR_SETUPS, PACKET_CAR_STATUS, PACKET_CAR_TELEMETRY,
    PACKET_EVENT, PACKET_FINAL_CLASSIFICATION, PACKET_LAP_DATA, PACKET_MOTION,
    PACKET_MOTION_EX, PACKET_SESSION, PACKET_SESSION_HISTORY, decode_event_code, decode_header,
    decode_packet, decode_player_car_telemetry, decode_player_lap_data, decode_player_setup,
    decode_session, supported_header,
)
from .personal_bests import PersonalBestRegistry
from .race_store import RaceSessionStore
from .reports import ExportError
from .setups import setup_rows, with_provenance
from .settings import SettingsStore

SESSION_UID_REOPEN_GUARD_SECONDS = 5.0
AUTOMATIC_RECORDING_MODES = {"practice", "qualifying", "race"}
TRACE_CACHE_MAX_BYTES = 128 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


class SessionStore:
    def __init__(self, capture_dir: Path) -> None:
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.capture_dir / "sessions"
        self.sessions_dir.mkdir(exist_ok=True)
        self.archive = SessionArchiveRepository(self.capture_dir)
        self.notes_path = self.archive.notes_path
        self.settings_store = SettingsStore(self.capture_dir / "settings.json")
        self.settings = self.settings_store.load()
        self.pb_registry = PersonalBestRegistry(self.capture_dir / "personal_bests")
        self.profile_store = CircuitProfileStore(self.capture_dir / "circuit_profiles")
        self.profile_service = CircuitProfileService(self.profile_store, self.archive)
        self.trash_service = SessionTrashService(self.archive, utc_now)
        self.settings_service = RuntimeSettingsService(self.settings_store, self.trash_service)
        self.amendment_service = SetupAmendmentService(self.archive, self.pb_registry, utc_now)
        self.diagnostics = DiagnosticCapture(
            self.capture_dir,
            lambda: int(self.settings["diagnostic_max_mb"]),
            lambda: MAX_DIAGNOSTIC_EVENTS,
            lambda: MAX_DIAGNOSTIC_SEGMENTS,
        )
        self.lock = Lock()
        self.latest: dict[str, Any] = {"connected": False}
        self.last_packet_at: float | None = None
        self.notes = self._load_notes()
        self.sessions: dict[str, RecordingSession] = {}
        self.session_laps: dict[str, list[Lap]] = {}
        self.laps: list[Lap] = []
        self.lap_by_id: dict[str, Lap] = {}
        self.session_lap_ids: dict[str, list[str]] = {}
        self.lap_summary_cache: dict[str, dict[str, Any]] = {}
        self.session_summary_cache: dict[str, dict[str, Any]] = {}
        self.trace_cache: OrderedDict[str, tuple[Lap, int]] = OrderedDict()
        self.trace_cache_bytes = 0
        self.game_session_uid: int | None = None
        self.game_mode = "unknown"
        self.game_session_type: int | None = None
        self.track_id: int | None = None
        self.track_length_m: int | None = None
        self.sector2_start_m: float | None = None
        self.sector3_start_m: float | None = None
        self.active_recording_id: str | None = None
        self.selected_session_id: str | None = None
        self.manual_stop_session_key: tuple[int, int] | None = None
        self.session_uid_reopen_guard: dict[int, float] = {}
        self.current_setup: dict[str, Any] | None = None
        self.recorder_state = RecorderState()
        self.recorder_service = RecorderApplicationService()
        self.race_store: RaceSessionStore | None = None
        self.packet_state: dict[int, dict[str, Any]] = {}
        self._purge_expired_trash_unlocked()
        self._load_all()

    # Temporary compatibility façade while callers migrate to RecorderState.
    # The state object is authoritative; these names preserve the established
    # SessionStore/test API without maintaining duplicate mutable state.
    @property
    def active_lap_number(self) -> int | None:
        return self.recorder_state.active_lap_number

    @active_lap_number.setter
    def active_lap_number(self, value: int | None) -> None:
        self.recorder_state.active_lap_number = value

    @property
    def active_invalid(self) -> bool:
        return self.recorder_state.invalid

    @active_invalid.setter
    def active_invalid(self, value: bool) -> None:
        self.recorder_state.invalid = value

    @property
    def active_samples(self) -> list[dict[str, Any]]:
        return self.recorder_state.samples

    @active_samples.setter
    def active_samples(self, value: list[dict[str, Any]]) -> None:
        self.recorder_state.samples = value

    @property
    def active_lap_events(self) -> list[dict[str, Any]]:
        return self.recorder_state.events

    @active_lap_events.setter
    def active_lap_events(self, value: list[dict[str, Any]]) -> None:
        self.recorder_state.events = value

    @property
    def pending_restart_event(self) -> dict[str, Any] | None:
        return self.recorder_state.pending_restart_event

    @pending_restart_event.setter
    def pending_restart_event(self, value: dict[str, Any] | None) -> None:
        self.recorder_state.pending_restart_event = value

    @property
    def active_lap_state(self) -> dict[str, Any]:
        return self.recorder_state.lap_packet

    @active_lap_state.setter
    def active_lap_state(self, value: dict[str, Any]) -> None:
        self.recorder_state.lap_packet = value

    @property
    def active_lap_setup(self) -> dict[str, Any] | None:
        return self.recorder_state.setup

    @active_lap_setup.setter
    def active_lap_setup(self, value: dict[str, Any] | None) -> None:
        self.recorder_state.setup = value

    @property
    def active_lap_context(self) -> dict[str, Any] | None:
        return self.recorder_state.race_context

    @active_lap_context.setter
    def active_lap_context(self, value: dict[str, Any] | None) -> None:
        self.recorder_state.race_context = value

    @property
    def capture_current_lap(self) -> bool:
        return self.recorder_state.capture_current_lap

    @capture_current_lap.setter
    def capture_current_lap(self, value: bool) -> None:
        self.recorder_state.capture_current_lap = value

    def _load_notes(self) -> dict[str, str]:
        return self.archive.load_notes()

    @staticmethod
    def _lap_from_payload(payload: dict[str, Any], defaults: dict[str, Any] | None = None) -> Lap:
        return SessionArchiveRepository.lap_from_payload(payload, defaults)

    def _load_all(self) -> None:
        contents = self.archive.load(mark_open_interrupted=True, interrupted_at=utc_now, lazy_traces=True)
        self.sessions = contents.sessions
        self.session_laps = contents.session_laps
        self.laps = contents.laps
        self._rebuild_lap_indexes()
        self.selected_session_id = contents.selected_session_id

    def _refresh_lap_index(self) -> None:
        self.laps = [lap for laps in self.session_laps.values() for lap in laps]
        self._rebuild_lap_indexes()

    def _rebuild_lap_indexes(self) -> None:
        self.lap_by_id = {lap.id: lap for lap in self.laps}
        self.session_lap_ids = {
            session_id: [lap.id for lap in laps]
            for session_id, laps in self.session_laps.items()
        }
        self.lap_summary_cache.clear()
        self.session_summary_cache.clear()
        self.trace_cache.clear()
        self.trace_cache_bytes = 0

    def _invalidate_lap(self, lap: Lap) -> None:
        self.lap_summary_cache.pop(lap.id, None)
        self.session_summary_cache.pop(lap.recording_session_id, None)
        cached = self.trace_cache.pop(lap.id, None)
        if cached:
            self.trace_cache_bytes -= cached[1]

    def _lap_summary(self, lap: Lap) -> dict[str, Any]:
        cached = self.lap_summary_cache.get(lap.id)
        note = self.notes.get(lap.id, "")
        if cached is None or cached.get("note") != note:
            cached = lap.summary(note)
            self.lap_summary_cache[lap.id] = cached
        return dict(cached)

    def _hydrate_lap(self, lap: Lap) -> Lap:
        if lap.samples:
            return lap
        cached = self.trace_cache.pop(lap.id, None)
        if cached is not None:
            self.trace_cache[lap.id] = cached
            return cached[0]
        hydrated = self.archive.load_full_lap(lap)
        try:
            size = self.archive.lap_path(lap).stat().st_size
        except OSError:
            size = len(hydrated.samples) * 256
        while self.trace_cache and self.trace_cache_bytes + size > TRACE_CACHE_MAX_BYTES:
            _key, (_old_lap, old_size) = self.trace_cache.popitem(last=False)
            self.trace_cache_bytes -= old_size
        if size <= TRACE_CACHE_MAX_BYTES:
            self.trace_cache[lap.id] = (hydrated, size)
            self.trace_cache_bytes += size
        return hydrated

    def _persist_session(self, session: RecordingSession) -> None:
        self.session_summary_cache.pop(session.id, None)
        self.archive.persist_session(session)

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
        if active is not None and active.mode in AUTOMATIC_RECORDING_MODES and previous_uid is not None:
            # At the end of an automatically recorded phase F1 25 can emit a
            # momentary different UID before a final bundle from the completed
            # UID. This is observed after both races and one-shot qualifying.
            # Do not let that trailing Session packet create an empty duplicate.
            self.session_uid_reopen_guard[previous_uid] = monotonic() + SESSION_UID_REOPEN_GUARD_SECONDS
        self._reset_lap()
        self.game_session_uid = session_uid
        self.game_mode, self.game_session_type = "unknown", None
        self.track_id, self.track_length_m = None, None
        self.sector2_start_m, self.sector3_start_m = None, None
        self.packet_state = {}
        self.current_setup = None
        self.manual_stop_session_key = None
        for key in (
            "setup", "front_wing", "rear_wing", "on_throttle_diff", "off_throttle_diff",
            "lap_number", "current_lap_ms", "lap_distance_m", "invalid",
        ):
            self.latest.pop(key, None)

    def record_game_session(self, game: dict[str, Any]) -> None:
        with self.lock:
            incoming_uid = int(game["session_uid"])
            incoming_type = int(game["session_type"])
            incoming_mode = str(game["mode"])
            previous_uid, previous_type = self.game_session_uid, self.game_session_type
            self._observe_uid(incoming_uid)
            phase_changed = bool(
                previous_uid == incoming_uid
                and previous_type is not None
                and previous_type != incoming_type
                and incoming_mode in AUTOMATIC_RECORDING_MODES
                and self.active_recording_id is not None
                and self.sessions[self.active_recording_id].mode in {"practice", "qualifying"}
            )
            if phase_changed:
                self._close_active("completed")
                self._reset_lap()
            self.game_session_uid = incoming_uid
            self.game_mode = incoming_mode
            self.game_session_type = incoming_type
            self.track_id = int(game["track_id"])
            self.track_length_m = int(game["track_length_m"])
            self.sector2_start_m = float(game["sector2_start_m"]) if game.get("sector2_start_m") is not None else None
            self.sector3_start_m = float(game["sector3_start_m"]) if game.get("sector3_start_m") is not None else None
            self.latest.update({
                "game_session_uid": self.game_session_uid, "game_mode": self.game_mode,
                "session_type": self.game_session_type, "track_id": self.track_id,
                "session_type_name": str(game.get("session_type_name") or self.game_mode.replace("_", " ").title()),
                "track_length_m": self.track_length_m, "track_name": track_name(self.track_id),
            })
            self._touch()
            now = monotonic()
            self.session_uid_reopen_guard = {
                uid: expires for uid, expires in self.session_uid_reopen_guard.items()
                if expires > now
            }
            guarded_tail = self.session_uid_reopen_guard.get(self.game_session_uid, 0) > now
            if (
                self.game_mode in AUTOMATIC_RECORDING_MODES
                and self.active_recording_id is None
                and self.manual_stop_session_key != (self.game_session_uid, self.game_session_type)
                and not guarded_tail
            ):
                session_name = str(game.get("session_type_name") or self.game_mode.replace("_", " ").title())
                self._create_recording(f"{session_name} · {track_name(self.track_id)}", self.game_mode, automatic=True)

    def game_session_ended(self, session_uid: int) -> None:
        with self.lock:
            if self.game_session_uid == session_uid:
                self._close_active("completed")
                self._reset_lap()
                if self.game_session_type is not None:
                    self.manual_stop_session_key = (session_uid, self.game_session_type)

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
        default_name = {"time_trial": "Time Trial run", "practice": "Practice", "qualifying": "Qualifying", "race": "Race"}.get(mode, "Session")
        session = RecordingSession(
            id=session_id, name=name.strip()[:80] or default_name,
            mode=mode, started_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            ended_at=None, status="recording" if at_start else "armed",
            game_session_uid=self.game_session_uid, track_id=self.track_id,
            track_length_m=self.track_length_m, lap_ids=[],
            game_session_type=self.game_session_type,
            game_session_name=str(self.latest.get("session_type_name") or default_name),
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
        if self.diagnostics.enabled:
            self.diagnostics.reset_recording()
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
        return DiagnosticCapture.timestamp()

    def diagnostic_status(self) -> dict[str, Any]:
        return self.diagnostics.status()

    def configure_diagnostics(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            self.diagnostics.set_enabled(enabled)
            if self.diagnostics.enabled and self.active_recording_id is not None and self.diagnostics.path is None:
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
        self.diagnostics.start_segment(self.active_recording_id, reason)

    def _write_diagnostic(self, event: dict[str, Any]) -> None:
        self.diagnostics.write(self.active_recording_id, event)

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
            if session.mode in AUTOMATIC_RECORDING_MODES and session.game_session_uid is not None and session.game_session_type is not None:
                self.manual_stop_session_key = (session.game_session_uid, session.game_session_type)
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
            result = self.trash_service.delete(
                session,
                self.session_laps.get(session_id, []),
                active_recording_id=self.active_recording_id,
                confirmed=confirmed,
            )
            assert session is not None
            self.sessions.pop(session.id)
            self.session_laps.pop(session.id, None)
            self._refresh_lap_index()
            if self.selected_session_id == session.id:
                remaining = [item for item in self.sessions.values() if item.id != LEGACY_SESSION_ID]
                self.selected_session_id = (
                    max(remaining, key=lambda item: item.started_at).id
                    if remaining else LEGACY_SESSION_ID
                )
            result["selected_session_id"] = self.selected_session_id
            return result

    def list_deleted_sessions(self) -> list[dict[str, Any]]:
        return self.trash_service.list(int(self.settings["trash_retention_days"]))

    def restore_deleted_session(self, trash_id: str, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise SessionStateError("Session restoration must be explicitly confirmed")
        with self.lock:
            session, laps = self.trash_service.restore(
                trash_id,
                confirmed=confirmed,
                existing_session_ids=set(self.sessions),
            )
            self.sessions[session.id], self.session_laps[session.id] = session, laps
            self.selected_session_id = session.id
            self._refresh_lap_index()
            return {"restored_session_id": session.id, "selected_session_id": session.id, "lap_count": len(laps)}

    def _purge_expired_trash_unlocked(self) -> dict[str, Any]:
        return self.trash_service.purge_expired(int(self.settings.get("trash_retention_days", 30)))

    def purge_expired_trash(self) -> dict[str, Any]:
        with self.lock:
            return self._purge_expired_trash_unlocked()

    def update_settings(self, values: Any) -> dict[str, Any]:
        with self.lock:
            self.settings, result = self.settings_service.update(self.settings, values)
            return result

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
        self.recorder_state.reset()

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
        """Apply one normalized Lap Data observation under the existing lock.

        The domain engine decides boundaries before this coordinator performs
        ordered persistence, active-buffer mutation, timeline and diagnostic
        effects. Packet dispatch order outside this method is unchanged.
        """
        with self.lock:
            self._observe_uid(session_uid)
            previous_packet = dict(self.active_lap_state)
            active_before = self.active_lap_number
            capture_before = self.capture_current_lap
            samples_before = len(self.active_samples)
            active_session = self.sessions.get(self.active_recording_id or "")
            status_before = active_session.status if active_session else None
            observation = LapDataObservation.from_packet(lap_state, session_uid, packet_meta)
            context = RecordingContext(
                recording_session_id=self.active_recording_id,
                mode=active_session.mode if active_session else None,
                game_session_type=active_session.game_session_type if active_session else None,
                status=active_session.status if active_session else None,
                track_length_m=self.track_length_m,
            )
            plan = decide_lap_boundary(self.recorder_state, observation, context)

            if self.diagnostics.enabled and plan.diagnostic_segment:
                self._start_diagnostic_segment(plan.diagnostic_segment)

            def mark_session_recording() -> None:
                if active_session is not None:
                    active_session.status = "recording"
                    self._persist_session(active_session)

            applied = self.recorder_service.apply(
                plan, self.recorder_state, observation,
                current_setup=self.current_setup,
                race_context=self._current_race_context,
                finish_lap=self._finish_lap,
                mark_session_recording=mark_session_recording,
            )
            decision = applied.code
            reasons = applied.reasons
            self.latest.update(lap_state)
            self._touch()
            active_session = self.sessions.get(self.active_recording_id or "")
            if self.race_store is not None and decision not in {
                DecisionCode.OBSERVE.value,
                DecisionCode.CONTINUE_CAPTURE.value,
                DecisionCode.CONTINUE_NOT_CAPTURING.value,
            }:
                self.race_store.append_event(
                    float(observation.session_time or 0), decision,
                    {"reasons": reasons, "flags": plan.flags, "packet": dict(lap_state)},
                    "recorder", plan.new_lap_number,
                )
            self._write_diagnostic({
                "event": "lap_data_decision", "timestamp_utc": self._diagnostic_timestamp(),
                "session_uid": session_uid,
                "session_time": observation.session_time,
                "frame_identifier": observation.frame_identifier,
                "overall_frame_identifier": observation.overall_frame_identifier,
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
                    "active_lap_number": self.active_lap_number,
                    "sample_count": len(self.active_samples),
                },
                "flags": plan.flags, "decision": decision, "reasons": reasons,
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
            if header.packet_id == PACKET_SESSION_HISTORY:
                self._consider_session_history(payload, header)
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
                session = self.sessions.get(self.active_recording_id or "")
                if session is not None:
                    session.final_classification = next(
                        (
                            dict(car) for car in payload.get("cars", [])
                            if car.get("car_index") == int(header.player_car_index)
                        ),
                        None,
                    )
                    if session.final_classification is not None:
                        self._persist_session(session)
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

    def _consider_session_history(self, payload: dict[str, Any], header: Any) -> None:
        session = self.sessions.get(self.active_recording_id or "")
        if (
            session is None or session.mode != "time_trial"
            or payload.get("car_index") != int(header.player_car_index)
            or session.track_id is None
        ):
            return
        for history in payload.get("laps", []):
            if not isinstance(history, dict):
                continue
            self.pb_registry.consider_official_history(
                track_id=session.track_id, track_length_m=session.track_length_m,
                mode=session.mode, time_ms=int(history.get("lap_time_ms") or 0),
                sector1_ms=_positive(history.get("sector1_ms")),
                sector2_ms=_positive(history.get("sector2_ms")),
                sector3_ms=_positive(history.get("sector3_ms")),
                lap_number=int(history.get("lap_number") or 0),
                valid_flags=int(history.get("valid_flags") or 0),
                session_id=session.id, session_name=session.name,
                game_session_uid=session.game_session_uid,
            )

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
        self.laps.append(lap)
        self.lap_by_id[lap.id] = lap
        self.session_lap_ids.setdefault(session.id, []).append(lap.id)
        self._invalidate_lap(lap)
        payload = asdict(lap)
        payload["sample_count"] = len(lap.samples)
        directory = self.sessions_dir / session.id
        self._atomic_json(directory / f"{lap.id}.json", payload, compact=True)
        self._persist_session(session)
        self.pb_registry.consider(lap, session.name, self.notes.get(lap.id, ""))
        if self.race_store is not None:
            # A completed lap is a useful durability boundary even though raw
            # packet writes are otherwise committed in short batches.
            self.race_store.flush()
        return True, "finish_saved_official_time_and_minimum_samples_present"

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            real = [session for session in self.sessions.values() if session.id != LEGACY_SESSION_ID]
            real.sort(key=lambda item: item.started_at, reverse=True)
            ordered = real + [self.sessions[LEGACY_SESSION_ID]]
            return [self._session_summary(session) for session in ordered]

    def list_sessions_page(
        self, page: int = 1, page_size: int = 25, *, year: str = "", month: str = "", day: str = "",
        mode: str = "", track_id: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            items = [session for session in self.sessions.values() if session.id != LEGACY_SESSION_ID]
            if year:
                items = [item for item in items if item.started_at[:4] == year]
            if month:
                items = [item for item in items if item.started_at[5:7] == month.zfill(2)]
            if day:
                items = [item for item in items if item.started_at[8:10] == day.zfill(2)]
            if mode:
                items = [item for item in items if item.mode == mode]
            if track_id is not None:
                items = [item for item in items if item.track_id == track_id]
            items.sort(key=lambda item: item.started_at, reverse=True)
            total = len(items)
            page_size = max(1, min(int(page_size), 100))
            pages = max(1, (total + page_size - 1) // page_size)
            page = max(1, min(int(page), pages))
            start = (page - 1) * page_size
            return {
                "sessions": [self._session_summary(item) for item in items[start:start + page_size]],
                "pagination": {"page": page, "page_size": page_size, "pages": pages, "total": total},
            }

    def _session_summary(self, session: RecordingSession) -> dict[str, Any]:
        cached = self.session_summary_cache.get(session.id)
        if cached is None:
            cached = session.summary(self.session_laps[session.id])
            self.session_summary_cache[session.id] = cached
        result = dict(cached)
        result["can_delete"] = (
            session.id != LEGACY_SESSION_ID
            and session.id != self.active_recording_id
            and session.status not in OPEN_STATUSES
        )
        if session.id == self.active_recording_id and self.race_store is not None:
            # The exact continuity audit scans the complete packet table. The
            # library and one-second live snapshot paths must stay O(1), or
            # they block the UDP receiver as a race database grows.
            result["continuous_store"] = self.race_store.live_summary()
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
            return self.profile_service.list_for_session(session)

    def save_profile(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            return self.profile_service.save(payload)

    def delete_profile(self, profile_id: str, confirmed: bool = False) -> dict[str, Any]:
        with self.lock:
            result = self.profile_service.delete(
                profile_id,
                confirmed=confirmed,
                sessions=self.sessions,
            )
            for session_id in result.get("cleared_session_ids", []):
                self.session_summary_cache.pop(session_id, None)
            return result

    def select_profile(self, session_id: str, profile_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            self.profile_service.select(session, profile_id)
            assert session is not None
            self.session_summary_cache.pop(session.id, None)
            return self._session_summary(session)

    def race_timeline(self, session_id: str, lap_from: int | None = None, lap_to: int | None = None) -> list[dict[str, Any]]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or not session.race_store_file:
                raise SessionStateError("Continuous race data is unavailable for this session")
            if session_id == self.active_recording_id and self.race_store is not None:
                events = self.race_store.live_timeline()
                return [
                    event for event in events
                    if (lap_from is None or event.get("lap_number") is None or event["lap_number"] >= lap_from)
                    and (lap_to is None or event.get("lap_number") is None or event["lap_number"] <= lap_to)
                ]
            path = self.sessions_dir / session_id / session.race_store_file
        # Closed-session reconstruction can be expensive. It uses its own
        # read-only SQLite connection and must not retain the recorder lock.
        if not path.exists():
            raise SessionStateError("Continuous race data is unavailable for this legacy session")
        return RaceSessionStore.read_timeline(path, lap_from, lap_to)

    def _race_timeline_unlocked(self, session_id: str, lap_from: int | None, lap_to: int | None) -> list[dict[str, Any]]:
        session = self.sessions.get(session_id)
        if session is None or not session.race_store_file:
            raise SessionStateError("Continuous race data is unavailable for this session")
        if session_id == self.active_recording_id and self.race_store is not None:
            events = self.race_store.live_timeline()
            return [
                event for event in events
                if (lap_from is None or event.get("lap_number") is None or event["lap_number"] >= lap_from)
                and (lap_to is None or event.get("lap_number") is None or event["lap_number"] <= lap_to)
            ]
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
        result = self.live_snapshot()
        with self.lock:
            result["laps"] = [
                self._lap_summary(lap)
                for lap in self.session_laps.get(self.selected_session_id or "", [])
            ]
        return result

    def live_snapshot(self) -> dict[str, Any]:
        """Return only O(1) state needed by the one-second live dashboard poll."""
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
            return [self._lap_summary(lap) for lap in self.session_laps[selected]]

    def list_all_laps(self) -> list[dict[str, Any]]:
        """Return all compact lap summaries in one response for initial cache hydration."""
        with self.lock:
            return [self._lap_summary(lap) for lap in self.laps]

    def list_laps_page(
        self, page: int = 1, page_size: int = 50, *, session_id: str | None = None,
        track_id: int | None = None, mode: str = "", validity: str = "",
    ) -> dict[str, Any]:
        with self.lock:
            if session_id is not None and session_id not in self.sessions:
                raise SessionStateError("Unknown session ID")
            items = self.session_laps[session_id] if session_id is not None else self.laps
            if track_id is not None:
                items = [lap for lap in items if lap.track_id == track_id]
            if mode:
                items = [lap for lap in items if lap.mode == mode]
            if validity == "valid":
                items = [lap for lap in items if not lap.invalid]
            elif validity == "invalid":
                items = [lap for lap in items if lap.invalid]
            total = len(items)
            page_size = max(1, min(int(page_size), 200))
            pages = max(1, (total + page_size - 1) // page_size)
            page = max(1, min(int(page), pages))
            start = (page - 1) * page_size
            return {
                "laps": [self._lap_summary(lap) for lap in items[start:start + page_size]],
                "pagination": {"page": page, "page_size": page_size, "pages": pages, "total": total},
            }

    def get_lap_object(self, lap_id: str) -> Lap | None:
        if lap_id.startswith("pb:"):
            entry = self.pb_registry.get(lap_id[3:])
            if entry is not None:
                try:
                    return self._lap_from_payload(entry["lap"])
                except (TypeError, KeyError, ValueError):
                    return None
        lap = self.lap_by_id.get(lap_id)
        if lap is None:
            return None
        try:
            return self._hydrate_lap(lap)
        except (OSError, ValueError, TypeError, KeyError):
            return None

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
        SessionArchiveRepository.atomic_json(path, payload, compact)

    def amend_lap_setup(self, lap_id: str, values: Any) -> dict[str, Any]:
        """Amend only setup metadata, retaining exact pre-change lap/PB backups."""
        with self.lock:
            lap = self.lap_by_id.get(lap_id)
            result = self.amendment_service.amend(lap, values, self.notes.get(lap_id, ""))
            assert lap is not None
            self.archive.invalidate_lap_index(lap)
            self._invalidate_lap(lap)
            return result

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
            self.archive.save_notes(self.notes)
            self.pb_registry.update_source_note(lap_id, clean_note)
            metadata = self.lap_by_id.get(lap_id)
            if metadata is not None:
                self._invalidate_lap(metadata)
            return {"id": lap_id, "note": clean_note}

    def list_personal_bests(self) -> list[dict[str, Any]]:
        with self.lock:
            return [entry | {"track_name": track_name(entry.get("track_id"))} for entry in self.pb_registry.summaries()]

    def personal_best(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return self.pb_registry.get(key)

    def rebuild_personal_bests(self) -> dict[str, int]:
        with self.lock:
            result = self.pb_registry.rebuild(
                [self._hydrate_lap(lap) for lap in self.laps],
                lambda session_id: self.sessions.get(session_id, self.sessions[LEGACY_SESSION_ID]).name,
                lambda lap_id: self.notes.get(lap_id, ""),
            )
            history_considered = history_updated = 0
            for session in self.sessions.values():
                if session.mode != "time_trial" or session.track_id is None or not session.race_store_file:
                    continue
                database = self.sessions_dir / session.id / session.race_store_file
                if not database.exists():
                    continue
                try:
                    payload = RaceSessionStore.read_latest_player_session_history(database)
                    histories = payload.get("laps", []) if payload else []
                except (OSError, sqlite3.Error, TypeError, ValueError):
                    continue
                for history in histories:
                    if not isinstance(history, dict):
                        continue
                    history_considered += 1
                    entry = self.pb_registry.consider_official_history(
                        track_id=session.track_id, track_length_m=session.track_length_m,
                        mode=session.mode, time_ms=int(history.get("lap_time_ms") or 0),
                        sector1_ms=_positive(history.get("sector1_ms")),
                        sector2_ms=_positive(history.get("sector2_ms")),
                        sector3_ms=_positive(history.get("sector3_ms")),
                        lap_number=int(history.get("lap_number") or 0),
                        valid_flags=int(history.get("valid_flags") or 0),
                        session_id=session.id, session_name=session.name,
                        game_session_uid=session.game_session_uid,
                    )
                    history_updated += entry is not None
            result["official_history_considered"] = history_considered
            result["official_history_updated"] = history_updated
            result["updated"] += history_updated
            return result

    def _raw_lap(self, lap: Lap) -> bytes | None:
        return self.archive.raw_lap(lap)

    def export_session(
        self, session_id: str, scope: str, lap_ids: list[str], export_format: str,
        report_type: str = "auto", *, file_output: bool = False,
    ) -> tuple[str, str, bytes | Path]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise ExportError("Unknown session ID")
            circuit_profile = self.profile_store.get(session.circuit_profile_id) if session.circuit_profile_id else None
            available = self.session_laps[session_id]
            if scope == "session":
                export_laps = [self._hydrate_lap(lap) for lap in available]
            else:
                requested = set(lap_ids)
                export_laps = [self._hydrate_lap(lap) for lap in available if lap.id in requested]
            snapshot = capture_session_export(
                archive=self.archive,
                sessions_dir=self.sessions_dir,
                session=session,
                available_laps=export_laps,
                notes=self.notes,
                circuit_profile=circuit_profile,
                active_store=self.race_store if session.id == self.active_recording_id else None,
                scope=scope,
                lap_ids=lap_ids,
                export_format=export_format,
                report_type=report_type,
            )
        return render_session_export(snapshot, file_output=file_output)

    def export_lap_analysis(self, baseline_id: str, candidate_id: str) -> tuple[str, str, bytes]:
        with self.lock:
            baseline, candidate = self.get_lap_object(baseline_id), self.get_lap_object(candidate_id)
            if baseline is None or candidate is None:
                missing = baseline_id if baseline is None else candidate_id
                raise ExportError(f"Unknown lap ID: {missing}")
            try:
                profile = resolve_comparison_profile(
                    baseline,
                    candidate,
                    self.sessions,
                    self.profile_store,
                )
            except ComparisonPolicyError as error:
                raise ExportError(str(error)) from error
            snapshot = CapturedComparisonExport(
                deepcopy(baseline),
                deepcopy(candidate),
                self.notes.get(baseline.id, ""),
                self.notes.get(candidate.id, ""),
                deepcopy(profile),
            )
        return render_comparison_export(snapshot)


class UdpReceiver(Thread):
    def __init__(self, store: SessionStore, port: int = 20777) -> None:
        super().__init__(daemon=True, name="f1-udp-receiver")
        self.store = store
        self.port = port
        self.stop_event = Event()

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # The default Linux UDP buffer is small enough to overflow during
            # brief filesystem or scheduler stalls. The kernel may clamp this
            # request to its configured maximum, but even that is preferable
            # to silently accepting the much smaller default.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
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
