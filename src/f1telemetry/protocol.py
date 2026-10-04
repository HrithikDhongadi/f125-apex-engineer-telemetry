"""Minimal, defensive decoder for the F1 25 (2025) UDP protocol."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

HEADER = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER.size
MAX_CARS = 22

PACKET_SESSION = 1
PACKET_LAP_DATA = 2
PACKET_EVENT = 3
PACKET_CAR_SETUPS = 5
PACKET_CAR_TELEMETRY = 6

# F1 25 keeps the F1 24 car-telemetry structure. Wheel order: RL, RR, FL, FR.
CAR_TELEMETRY_SIZE = 60
LAP_DATA_SIZE = 57
CAR_SETUP_SIZE = 50
SESSION_PACKET_VERSION = 1


@dataclass(frozen=True)
class PacketHeader:
    packet_format: int
    game_year: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame_identifier: int
    overall_frame_identifier: int
    player_car_index: int


def decode_header(data: bytes) -> PacketHeader | None:
    if len(data) < HEADER_SIZE:
        return None
    (
        packet_format,
        game_year,
        _major,
        _minor,
        packet_version,
        packet_id,
        session_uid,
        session_time,
        frame_identifier,
        overall_frame_identifier,
        player_car_index,
        _secondary_player,
    ) = HEADER.unpack_from(data)
    return PacketHeader(
        packet_format=packet_format,
        game_year=game_year,
        packet_version=packet_version,
        packet_id=packet_id,
        session_uid=session_uid,
        session_time=session_time,
        frame_identifier=frame_identifier,
        overall_frame_identifier=overall_frame_identifier,
        player_car_index=player_car_index,
    )


def _player_offset(header: PacketHeader, item_size: int, data: bytes) -> int | None:
    if header.player_car_index >= MAX_CARS:
        return None
    offset = HEADER_SIZE + header.player_car_index * item_size
    return offset if len(data) >= offset + item_size else None


def decode_player_car_telemetry(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    offset = _player_offset(header, CAR_TELEMETRY_SIZE, data)
    if offset is None:
        return None

    # Exact fixed offsets from CarTelemetryData, packed little-endian.
    speed = struct.unpack_from("<H", data, offset)[0]
    throttle, steer, brake = struct.unpack_from("<fff", data, offset + 2)
    gear = struct.unpack_from("<b", data, offset + 15)[0]
    rpm = struct.unpack_from("<H", data, offset + 16)[0]
    drs = bool(data[offset + 18])
    brake_temps = struct.unpack_from("<4H", data, offset + 22)
    tyre_surface = tuple(data[offset + 30 : offset + 34])
    tyre_inner = tuple(data[offset + 34 : offset + 38])
    tyre_pressures = struct.unpack_from("<4f", data, offset + 40)
    return {
        "speed_kph": speed,
        "throttle": round(throttle * 100, 1),
        "brake": round(brake * 100, 1),
        "steering": round(steer * 100, 1),
        "gear": gear,
        "rpm": rpm,
        "drs": drs,
        "brake_temps_c": brake_temps,
        "tyre_surface_c": tyre_surface,
        "tyre_inner_c": tyre_inner,
        "tyre_pressures_psi": tuple(round(value, 2) for value in tyre_pressures),
    }


def decode_player_lap_data(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    offset = _player_offset(header, LAP_DATA_SIZE, data)
    if offset is None:
        return None
    last_lap_ms, current_lap_ms = struct.unpack_from("<II", data, offset)
    sector1_ms = struct.unpack_from("<H", data, offset + 8)[0]
    sector1_minutes = data[offset + 10]
    sector2_ms = struct.unpack_from("<H", data, offset + 11)[0]
    sector2_minutes = data[offset + 13]
    lap_distance, total_distance = struct.unpack_from("<ff", data, offset + 20)
    return {
        "last_lap_ms": last_lap_ms,
        "current_lap_ms": current_lap_ms,
        "sector1_ms": sector1_minutes * 60_000 + sector1_ms,
        "sector2_ms": sector2_minutes * 60_000 + sector2_ms,
        "lap_distance_m": round(lap_distance, 2),
        "total_distance_m": round(total_distance, 2),
        "position": data[offset + 32],
        "lap_number": data[offset + 33],
        "sector": data[offset + 36],
        "invalid": bool(data[offset + 37]),
    }


def decode_player_setup(data: bytes, header: PacketHeader) -> dict[str, int | float] | None:
    offset = _player_offset(header, CAR_SETUP_SIZE, data)
    if offset is None:
        return None
    front_camber, rear_camber, front_toe, rear_toe = struct.unpack_from("<4f", data, offset + 4)
    rear_left, rear_right, front_left, front_right = struct.unpack_from("<4f", data, offset + 29)
    return {
        "front_wing": data[offset],
        "rear_wing": data[offset + 1],
        "on_throttle_diff": data[offset + 2],
        "off_throttle_diff": data[offset + 3],
        "front_camber": round(front_camber, 3),
        "rear_camber": round(rear_camber, 3),
        "front_toe": round(front_toe, 3),
        "rear_toe": round(rear_toe, 3),
        "front_suspension": data[offset + 20],
        "rear_suspension": data[offset + 21],
        "front_anti_roll_bar": data[offset + 22],
        "rear_anti_roll_bar": data[offset + 23],
        "front_ride_height": data[offset + 24],
        "rear_ride_height": data[offset + 25],
        "brake_pressure": data[offset + 26],
        "brake_bias": data[offset + 27],
        "engine_braking": data[offset + 28],
        "rear_left_tyre_pressure_psi": round(rear_left, 3),
        "rear_right_tyre_pressure_psi": round(rear_right, 3),
        "front_left_tyre_pressure_psi": round(front_left, 3),
        "front_right_tyre_pressure_psi": round(front_right, 3),
        "ballast": data[offset + 45],
        "fuel_load_kg": round(struct.unpack_from("<f", data, offset + 46)[0], 3),
    }


def decode_session(data: bytes, header: PacketHeader) -> dict[str, int | str] | None:
    """Decode the stable leading fields of the F1 25 Session packet."""
    if (
        header.packet_format != 2025
        or header.game_year != 25
        or header.packet_id != PACKET_SESSION
        or header.packet_version != SESSION_PACKET_VERSION
        or len(data) < HEADER_SIZE + 8
    ):
        return None
    track_length_m = struct.unpack_from("<H", data, HEADER_SIZE + 4)[0]
    session_type = data[HEADER_SIZE + 6]
    track_id = struct.unpack_from("<b", data, HEADER_SIZE + 7)[0]
    mode = "time_trial" if session_type == 18 else "race" if 15 <= session_type <= 17 else "unknown"
    return {
        "session_uid": header.session_uid,
        "session_type": session_type,
        "mode": mode,
        "track_id": track_id,
        "track_length_m": track_length_m,
    }


def decode_event_code(data: bytes, header: PacketHeader) -> str | None:
    if (
        header.packet_format != 2025
        or header.game_year != 25
        or header.packet_id != PACKET_EVENT
        or len(data) < HEADER_SIZE + 4
    ):
        return None
    return data[HEADER_SIZE:HEADER_SIZE + 4].decode("ascii", errors="ignore")
