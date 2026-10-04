"""UDP receiver, session state and local clean-lap recording."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
from typing import Any

from .protocol import (
    PACKET_CAR_SETUPS,
    PACKET_CAR_TELEMETRY,
    PACKET_LAP_DATA,
    decode_header,
    decode_player_car_telemetry,
    decode_player_lap_data,
    decode_player_setup,
)


@dataclass
class Lap:
    number: int
    time_ms: int
    invalid: bool
    samples: list[dict[str, Any]] = field(default_factory=list)
    setup: dict[str, int] | None = None
    saved_at: str = ""

    @property
    def id(self) -> str:
        return f"lap-{self.saved_at}-{self.number}"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "time_ms": self.time_ms,
            "invalid": self.invalid,
            "sample_count": len(self.samples),
            "setup": self.setup,
        }


class SessionStore:
    def __init__(self, capture_dir: Path) -> None:
        self.capture_dir = capture_dir
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()
        self.active_lap_number: int | None = None
        self.active_invalid = False
        self.active_samples: list[dict[str, Any]] = []
        self.current_setup: dict[str, int] | None = None
        self.latest: dict[str, Any] = {"connected": False}
        self.laps: list[Lap] = []

    def record_setup(self, setup: dict[str, int]) -> None:
        with self.lock:
            self.current_setup = setup

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
            self.active_invalid = self.active_invalid or lap_state["invalid"]
            self.latest.update(lap_state)
            self.latest["connected"] = True

    def record_telemetry(self, telemetry: dict[str, Any], session_time: float) -> None:
        with self.lock:
            if self.active_lap_number is None:
                return
            sample = {"t": round(session_time, 3), **telemetry}
            self.active_samples.append(sample)
            self.latest.update(sample)
            self.latest["connected"] = True

    def _finish_lap(self, time_ms: int) -> None:
        # Ignore formation/out-laps and incomplete records.
        if time_ms < 30_000 or len(self.active_samples) < 30:
            return
        lap = Lap(
            number=self.active_lap_number or 0,
            time_ms=time_ms,
            invalid=self.active_invalid,
            samples=self.active_samples,
            setup=self.current_setup,
            saved_at=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
        self.laps.append(lap)
        filename = self.capture_dir / f"{lap.id}.json"
        filename.write_text(json.dumps(asdict(lap), separators=(",", ":")), encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"latest": dict(self.latest), "laps": [lap.summary() for lap in self.laps[-20:]]}

    def lap(self, lap_id: str) -> dict[str, Any] | None:
        with self.lock:
            for lap in self.laps:
                if lap.id == lap_id:
                    return asdict(lap) | {"id": lap.id}
        return None


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
