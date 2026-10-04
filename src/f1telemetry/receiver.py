"""UDP receiver, session state, and durable local lap recording."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from .protocol import (
    PACKET_CAR_SETUPS, PACKET_CAR_TELEMETRY, PACKET_LAP_DATA,
    decode_header, decode_player_car_telemetry, decode_player_lap_data, decode_player_setup,
)


@dataclass
class Lap:
    number: int
    time_ms: int
    invalid: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
    setup: dict[str, int] | None = None
    saved_at: str = ""
    sector1_ms: int | None = None
    sector2_ms: int | None = None
    first_session_time: float | None = None
    last_session_time: float | None = None
    peak_speed_kph: int | None = None
    minimum_speed_kph: int | None = None
    max_brake_temps_c: list[int] | None = None
    max_tyre_inner_c: list[int] | None = None

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
        }


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _wheel_max(samples: list[dict[str, Any]], key: str) -> list[int] | None:
    rows = [
        values for sample in samples
        if isinstance((values := sample.get(key)), (list, tuple)) and len(values) == 4
    ]
    return [max(int(row[index]) for row in rows) for index in range(4)] if rows else None


def enrich_lap(lap: Lap) -> Lap:
    """Fill derived metadata, including for captures written by v0.1."""
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


class SessionStore:
    def __init__(self, capture_dir: Path) -> None:
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.notes_path = self.capture_dir / "notes.json"
        self.lock = Lock()
        self.active_lap_number: int | None = None
        self.active_invalid = False
        self.active_samples: list[dict[str, Any]] = []
        self.active_lap_state: dict[str, Any] = {}
        self.current_setup: dict[str, int] | None = None
        self.latest: dict[str, Any] = {"connected": False}
        self.last_packet_at: float | None = None
        self.notes = self._load_notes()
        self.laps = self._load_laps()

    def _load_notes(self) -> dict[str, str]:
        try:
            payload = json.loads(self.notes_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in payload.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _load_laps(self) -> list[Lap]:
        laps: list[Lap] = []
        allowed = {item.name for item in fields(Lap)}
        for path in sorted(self.capture_dir.glob("lap-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                lap = Lap(**{key: value for key, value in payload.items() if key in allowed})
                laps.append(enrich_lap(lap))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return laps

    def record_setup(self, setup: dict[str, int]) -> None:
        with self.lock:
            self.current_setup = dict(setup)
            self.latest.update(setup)
            self.latest["setup"] = dict(setup)
            self.latest["connected"] = True
            self.last_packet_at = monotonic()

    def record_lap_state(self, lap_state: dict[str, Any]) -> None:
        with self.lock:
            lap_number = lap_state["lap_number"]
            if self.active_lap_number is None:
                self.active_lap_number = lap_number
            elif lap_number != self.active_lap_number:
                self._finish_lap(lap_state["last_lap_ms"])
                self.active_lap_number = lap_number
                self.active_invalid = False
                self.active_samples = []
                self.active_lap_state = {}
            self.active_invalid = self.active_invalid or lap_state["invalid"]
            self.active_lap_state.update(lap_state)
            self.latest.update(lap_state)
            self.latest["connected"] = True
            self.last_packet_at = monotonic()

    def record_telemetry(self, telemetry: dict[str, Any], session_time: float) -> None:
        with self.lock:
            sample = {
                "t": round(session_time, 3),
                "lap_distance_m": self.active_lap_state.get("lap_distance_m"),
                "current_lap_ms": self.active_lap_state.get("current_lap_ms"),
                **telemetry,
            }
            if self.active_lap_number is not None:
                self.active_samples.append(sample)
            self.latest.update(sample)
            self.latest["connected"] = True
            self.last_packet_at = monotonic()

    def _finish_lap(self, time_ms: int) -> None:
        if time_ms < 30_000 or len(self.active_samples) < 30:
            return
        state = self.active_lap_state
        lap = enrich_lap(Lap(
            number=self.active_lap_number or 0,
            time_ms=time_ms,
            invalid=self.active_invalid,
            samples=self.active_samples,
            setup=dict(self.current_setup) if self.current_setup else None,
            saved_at=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            sector1_ms=_positive(state.get("sector1_ms")),
            sector2_ms=_positive(state.get("sector2_ms")),
        ))
        self.laps.append(lap)
        payload = asdict(lap)
        payload["sample_count"] = len(lap.samples)
        (self.capture_dir / f"{lap.id}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            summaries = [lap.summary(self.notes.get(lap.id, "")) for lap in self.laps[-50:]]
            latest = dict(self.latest)
            latest["connected"] = self.last_packet_at is not None and monotonic() - self.last_packet_at < 3.0
            return {"latest": latest, "laps": summaries}

    def list_laps(self) -> list[dict[str, Any]]:
        with self.lock:
            return [lap.summary(self.notes.get(lap.id, "")) for lap in self.laps]

    def get_lap_object(self, lap_id: str) -> Lap | None:
        return next((lap for lap in self.laps if lap.id == lap_id), None)

    def lap(self, lap_id: str) -> dict[str, Any] | None:
        with self.lock:
            lap = self.get_lap_object(lap_id)
            if lap:
                return asdict(lap) | {"id": lap.id, "sample_count": len(lap.samples), "note": self.notes.get(lap.id, "")}
        return None

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
            return {"id": lap_id, "note": clean_note}


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
        if header is None:
            return
        if header.packet_id == PACKET_LAP_DATA:
            lap_state = decode_player_lap_data(data, header)
            if lap_state:
                self.store.record_lap_state(lap_state)
        elif header.packet_id == PACKET_CAR_TELEMETRY:
            telemetry = decode_player_car_telemetry(data, header)
            if telemetry:
                self.store.record_telemetry(telemetry, header.session_time)
        elif header.packet_id == PACKET_CAR_SETUPS:
            setup = decode_player_setup(data, header)
            if setup:
                self.store.record_setup(setup)

    def stop(self) -> None:
        self.stop_event.set()
