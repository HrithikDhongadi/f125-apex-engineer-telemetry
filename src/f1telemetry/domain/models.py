"""Stable lap and recording-session models.

These fields and summary shapes are persisted and exposed through the existing
API, so structural refactors must preserve them unless a migration is approved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..circuits import is_known_track, track_name
from ..quality import lap_quality


LEGACY_SESSION_ID = "legacy"


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
        context = self.race_context if isinstance(self.race_context, dict) else {}
        start = context.get("start") if isinstance(context.get("start"), dict) else {}
        finish = context.get("finish") if isinstance(context.get("finish"), dict) else {}
        start_status = start.get("car_status") if isinstance(start.get("car_status"), dict) else {}
        finish_status = finish.get("car_status") if isinstance(finish.get("car_status"), dict) else {}
        start_lap = start.get("lap") if isinstance(start.get("lap"), dict) else {}
        finish_lap = finish.get("lap") if isinstance(finish.get("lap"), dict) else {}
        start_session = start.get("session") if isinstance(start.get("session"), dict) else {}
        start_damage = start.get("damage") if isinstance(start.get("damage"), dict) else {}
        finish_damage = finish.get("damage") if isinstance(finish.get("damage"), dict) else {}
        return {
            "id": self.id, "number": self.number, "time_ms": self.time_ms,
            "invalid": self.invalid, "sample_count": len(self.samples) or int(getattr(self, "_sample_count", 0)),
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
            "actual_tyre_compound": start_status.get("actual_tyre_compound"),
            "visual_tyre_compound": start_status.get("visual_tyre_compound"),
            "tyre_age_laps": start_status.get("tyre_age_laps"),
            "start_fuel_kg": start_status.get("fuel_kg"),
            "finish_fuel_kg": finish_status.get("fuel_kg"),
            "start_position": start_lap.get("position"),
            "finish_position": finish_lap.get("position"),
            "grid_position": start_lap.get("grid_position"),
            "start_pit_stops": start_lap.get("pit_stops"),
            "finish_pit_stops": finish_lap.get("pit_stops"),
            "start_penalties_s": start_lap.get("penalties_s"),
            "finish_penalties_s": finish_lap.get("penalties_s"),
            "start_warnings": start_lap.get("warnings"),
            "finish_warnings": finish_lap.get("warnings"),
            "weather": start_session.get("weather"),
            "race_total_laps": start_session.get("total_laps"),
            "finish_result_status": finish_lap.get("result_status"),
            "track_temperature_c": start_session.get("track_temperature_c"),
            "air_temperature_c": start_session.get("air_temperature_c"),
            "start_tyre_wear_pct": start_damage.get("tyre_wear_pct"),
            "finish_tyre_wear_pct": finish_damage.get("tyre_wear_pct"),
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
    game_session_type: int | None = None
    game_session_name: str | None = None
    track_name_override: str | None = None
    packet_format: int = 2025
    sector2_start_m: float | None = None
    sector3_start_m: float | None = None
    circuit_profile_id: str | None = None
    race_store_file: str | None = None
    final_classification: dict[str, Any] | None = None

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


def _wheel_max(samples: list[dict[str, Any]], key: str) -> list[int] | None:
    rows = [
        values for sample in samples
        if isinstance((values := sample.get(key)), (list, tuple)) and len(values) == 4
    ]
    return [max(int(row[index]) for row in rows) for index in range(4)] if rows else None


def enrich_lap(lap: Lap) -> Lap:
    """Populate derived legacy-compatible fields without changing raw samples."""
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
