"""Minimal, defensive decoder for the F1 25 (2025) UDP protocol."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

HEADER = struct.Struct("<HBBBBBQfIIBB")
HEADER_SIZE = HEADER.size
MAX_CARS = 22

PACKET_SESSION = 1
PACKET_MOTION = 0
PACKET_LAP_DATA = 2
PACKET_EVENT = 3
PACKET_PARTICIPANTS = 4
PACKET_CAR_SETUPS = 5
PACKET_CAR_TELEMETRY = 6
PACKET_CAR_STATUS = 7
PACKET_FINAL_CLASSIFICATION = 8
PACKET_CAR_DAMAGE = 10
PACKET_SESSION_HISTORY = 11
PACKET_TYRE_SETS = 12
PACKET_MOTION_EX = 13
PACKET_LAP_POSITIONS = 15

# F1 25 keeps the F1 24 car-telemetry structure. Wheel order: RL, RR, FL, FR.
CAR_TELEMETRY_SIZE = 60
LAP_DATA_SIZE = 57
CAR_SETUP_SIZE = 50
SESSION_PACKET_VERSION = 1
SUPPORTED_VERSIONS = {packet_id: 1 for packet_id in (
    PACKET_MOTION, PACKET_SESSION, PACKET_LAP_DATA, PACKET_EVENT,
    PACKET_PARTICIPANTS, PACKET_CAR_SETUPS, PACKET_CAR_TELEMETRY,
    PACKET_CAR_STATUS, PACKET_FINAL_CLASSIFICATION, PACKET_CAR_DAMAGE,
    PACKET_SESSION_HISTORY, PACKET_TYRE_SETS, PACKET_MOTION_EX,
    PACKET_LAP_POSITIONS,
)}


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
    game_major_version: int = 0
    game_minor_version: int = 0
    secondary_player_car_index: int = 255


def decode_header(data: bytes) -> PacketHeader | None:
    if len(data) < HEADER_SIZE:
        return None
    (
        packet_format,
        game_year,
        major,
        minor,
        packet_version,
        packet_id,
        session_uid,
        session_time,
        frame_identifier,
        overall_frame_identifier,
        player_car_index,
        secondary_player,
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
        game_major_version=major,
        game_minor_version=minor,
        secondary_player_car_index=secondary_player,
    )


def supported_header(header: PacketHeader) -> bool:
    """True only for byte layouts explicitly supported by this decoder."""
    return (
        header.packet_format == 2025
        and header.game_year == 25
        and SUPPORTED_VERSIONS.get(header.packet_id) == header.packet_version
    )


def header_dict(header: PacketHeader) -> dict[str, Any]:
    return {
        "packet_format": header.packet_format,
        "game_year": header.game_year,
        "game_major_version": header.game_major_version,
        "game_minor_version": header.game_minor_version,
        "packet_version": header.packet_version,
        "packet_id": header.packet_id,
        "session_uid": header.session_uid,
        "session_time": header.session_time,
        "frame_identifier": header.frame_identifier,
        "overall_frame_identifier": header.overall_frame_identifier,
        "player_car_index": header.player_car_index,
        "secondary_player_car_index": header.secondary_player_car_index,
    }


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
        "pit_status": data[offset + 34],
        "pit_stops": data[offset + 35],
        "sector": data[offset + 36],
        "invalid": bool(data[offset + 37]),
        "penalties_s": data[offset + 38],
        "grid_position": data[offset + 43],
        "driver_status": data[offset + 44],
        "result_status": data[offset + 45],
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
    result: dict[str, int | float | str] = {
        "session_uid": header.session_uid,
        "session_type": session_type,
        "mode": mode,
        "track_id": track_id,
        "track_length_m": track_length_m,
    }
    if len(data) >= 753:
        result.update({
            "weather": data[HEADER_SIZE],
            "track_temperature_c": struct.unpack_from("<b", data, HEADER_SIZE + 1)[0],
            "air_temperature_c": struct.unpack_from("<b", data, HEADER_SIZE + 2)[0],
            "total_laps": data[HEADER_SIZE + 3],
            "safety_car_status": data[HEADER_SIZE + 124],
            "sector2_start_m": round(struct.unpack_from("<f", data, 745)[0], 3),
            "sector3_start_m": round(struct.unpack_from("<f", data, 749)[0], 3),
        })
    return result


def decode_event_code(data: bytes, header: PacketHeader) -> str | None:
    if (
        header.packet_format != 2025
        or header.game_year != 25
        or header.packet_id != PACKET_EVENT
        or len(data) < HEADER_SIZE + 4
    ):
        return None
    return data[HEADER_SIZE:HEADER_SIZE + 4].decode("ascii", errors="ignore")


def _cars(data: bytes, item_size: int, start: int = HEADER_SIZE) -> list[int]:
    return list(range(min(MAX_CARS, max(0, (len(data) - start) // item_size))))


def decode_motion(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_MOTION or len(data) < 1349:
        return None
    cars = []
    for index in _cars(data, 60):
        offset = HEADER_SIZE + index * 60
        values = struct.unpack_from("<6f6h6f", data, offset)
        cars.append({
            "car_index": index,
            "world_x": values[0], "world_y": values[1], "world_z": values[2],
            "velocity_x": values[3], "velocity_y": values[4], "velocity_z": values[5],
            "forward_x": values[6] / 32767.0, "forward_y": values[7] / 32767.0,
            "forward_z": values[8] / 32767.0,
            "g_lateral": values[12], "g_longitudinal": values[13], "g_vertical": values[14],
            "yaw": values[15], "pitch": values[16], "roll": values[17],
        })
    return {"cars": cars, "player": cars[header.player_car_index] if header.player_car_index < len(cars) else None}


def _lap_at(data: bytes, index: int) -> dict[str, Any]:
    offset = HEADER_SIZE + index * LAP_DATA_SIZE
    last_lap_ms, current_lap_ms = struct.unpack_from("<II", data, offset)
    s1 = struct.unpack_from("<H", data, offset + 8)[0] + data[offset + 10] * 60_000
    s2 = struct.unpack_from("<H", data, offset + 11)[0] + data[offset + 13] * 60_000
    delta_front = struct.unpack_from("<H", data, offset + 14)[0] + data[offset + 16] * 60_000
    delta_leader = struct.unpack_from("<H", data, offset + 17)[0] + data[offset + 19] * 60_000
    lap_distance, total_distance, safety_delta = struct.unpack_from("<fff", data, offset + 20)
    return {
        "car_index": index, "last_lap_ms": last_lap_ms, "current_lap_ms": current_lap_ms,
        "sector1_ms": s1, "sector2_ms": s2, "lap_distance_m": lap_distance,
        "delta_to_car_in_front_ms": delta_front, "delta_to_race_leader_ms": delta_leader,
        "total_distance_m": total_distance, "safety_car_delta_s": safety_delta,
        "position": data[offset + 32], "lap_number": data[offset + 33],
        "pit_status": data[offset + 34], "pit_stops": data[offset + 35],
        "sector": data[offset + 36], "invalid": bool(data[offset + 37]),
        "penalties_s": data[offset + 38], "warnings": data[offset + 39],
        "grid_position": data[offset + 43], "driver_status": data[offset + 44],
        "result_status": data[offset + 45], "pit_lane_timer_active": bool(data[offset + 46]),
        "pit_lane_time_ms": struct.unpack_from("<H", data, offset + 47)[0],
        "pit_stop_time_ms": struct.unpack_from("<H", data, offset + 49)[0],
    }


def decode_all_lap_data(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_LAP_DATA or len(data) < 1285:
        return None
    cars = [_lap_at(data, index) for index in range(MAX_CARS)]
    return {"cars": cars, "player": cars[header.player_car_index] if header.player_car_index < MAX_CARS else None}


def decode_event(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    code = decode_event_code(data, header)
    if code is None:
        return None
    offset = HEADER_SIZE + 4
    result: dict[str, Any] = {"code": code, "source": "udp_event"}
    if code == "PENA" and len(data) >= offset + 7:
        keys = ("penalty_type", "infringement_type", "vehicle_index", "other_vehicle_index", "time_s", "lap_number", "places_gained")
        result.update(dict(zip(keys, data[offset:offset + 7])))
    elif code == "OVTK" and len(data) >= offset + 2:
        result.update({"overtaking_vehicle_index": data[offset], "overtaken_vehicle_index": data[offset + 1]})
    elif code == "SCAR" and len(data) >= offset + 2:
        result.update({"safety_car_type": data[offset], "event_type": data[offset + 1]})
    elif code == "FLBK" and len(data) >= offset + 8:
        frame, session_time = struct.unpack_from("<If", data, offset)
        result.update({"flashback_frame_identifier": frame, "flashback_session_time": session_time})
    elif code in {"RTMT", "TMPT", "RCWN", "DTSV"} and len(data) > offset:
        result["vehicle_index"] = data[offset]
        if code == "RTMT" and len(data) >= offset + 2:
            result["reason"] = data[offset + 1]
    elif code == "FTLP" and len(data) >= offset + 5:
        result.update({"vehicle_index": data[offset], "lap_time_s": struct.unpack_from("<f", data, offset + 1)[0]})
    elif code == "STLG" and len(data) > offset:
        result["lights"] = data[offset]
    elif code == "SGSV" and len(data) >= offset + 5:
        result.update({"vehicle_index": data[offset], "stop_time_s": struct.unpack_from("<f", data, offset + 1)[0]})
    elif code == "COLL" and len(data) >= offset + 2:
        result.update({"vehicle_1_index": data[offset], "vehicle_2_index": data[offset + 1]})
    elif code == "DRSD" and len(data) > offset:
        result["reason"] = data[offset]
    elif code == "BUTN" and len(data) >= offset + 4:
        result["button_status"] = struct.unpack_from("<I", data, offset)[0]
    return result


def decode_participants(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_PARTICIPANTS or len(data) < 1284:
        return None
    count = min(data[HEADER_SIZE], MAX_CARS)
    participants = []
    for index in range(count):
        offset = HEADER_SIZE + 1 + index * 57
        name = data[offset + 7:offset + 39].split(b"\0", 1)[0].decode("utf-8", errors="replace")
        participants.append({
            "car_index": index, "ai_controlled": bool(data[offset]), "driver_id": data[offset + 1],
            "network_id": data[offset + 2], "team_id": data[offset + 3], "race_number": data[offset + 5],
            "nationality": data[offset + 6], "name": name, "telemetry_public": bool(data[offset + 39]),
            "platform": data[offset + 43],
        })
    return {"active_cars": count, "participants": participants}


def decode_car_status(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_CAR_STATUS or len(data) < 1239:
        return None
    cars = []
    for index in range(MAX_CARS):
        offset = HEADER_SIZE + index * 55
        fuel, capacity, remaining = struct.unpack_from("<fff", data, offset + 5)
        cars.append({
            "car_index": index, "traction_control": data[offset], "abs": bool(data[offset + 1]),
            "fuel_mix": data[offset + 2], "front_brake_bias": data[offset + 3],
            "pit_limiter": bool(data[offset + 4]), "fuel_kg": fuel, "fuel_capacity_kg": capacity,
            "fuel_remaining_laps": remaining, "actual_tyre_compound": data[offset + 25],
            "visual_tyre_compound": data[offset + 26], "tyre_age_laps": data[offset + 27],
            "fia_flag": struct.unpack_from("<b", data, offset + 28)[0],
            "ers_store_j": struct.unpack_from("<f", data, offset + 37)[0],
            "ers_deploy_mode": data[offset + 41], "ers_deployed_this_lap_j": struct.unpack_from("<f", data, offset + 50)[0],
        })
    return {"cars": cars, "player": cars[header.player_car_index] if header.player_car_index < MAX_CARS else None}


def decode_car_damage(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_CAR_DAMAGE or len(data) < 1041:
        return None
    cars = []
    for index in range(MAX_CARS):
        offset = HEADER_SIZE + index * 46
        cars.append({
            "car_index": index, "tyre_wear_pct": list(struct.unpack_from("<4f", data, offset)),
            "tyre_damage_pct": list(data[offset + 16:offset + 20]),
            "brake_damage_pct": list(data[offset + 20:offset + 24]),
            "tyre_blisters_pct": list(data[offset + 24:offset + 28]),
            "front_left_wing_damage_pct": data[offset + 28], "front_right_wing_damage_pct": data[offset + 29],
            "rear_wing_damage_pct": data[offset + 30], "floor_damage_pct": data[offset + 31],
            "diffuser_damage_pct": data[offset + 32], "sidepod_damage_pct": data[offset + 33],
            "drs_fault": bool(data[offset + 34]), "ers_fault": bool(data[offset + 35]),
            "gearbox_damage_pct": data[offset + 36], "engine_damage_pct": data[offset + 37],
        })
    return {"cars": cars, "player": cars[header.player_car_index] if header.player_car_index < MAX_CARS else None}


def decode_final_classification(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_FINAL_CLASSIFICATION or len(data) < 1042:
        return None
    count = min(data[HEADER_SIZE], MAX_CARS)
    rows = []
    for index in range(count):
        offset = HEADER_SIZE + 1 + index * 46
        rows.append({
            "car_index": index, "position": data[offset], "laps": data[offset + 1],
            "grid_position": data[offset + 2], "points": data[offset + 3], "pit_stops": data[offset + 4],
            "result_status": data[offset + 5], "result_reason": data[offset + 6],
            "best_lap_ms": struct.unpack_from("<I", data, offset + 7)[0],
            "total_race_time_s": struct.unpack_from("<d", data, offset + 11)[0],
            "penalties_s": data[offset + 19], "penalty_count": data[offset + 20],
        })
    return {"cars": rows}


def decode_session_history(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_SESSION_HISTORY or len(data) < 1460:
        return None
    offset = HEADER_SIZE
    car_index, num_laps, num_stints = data[offset:offset + 3]
    laps = []
    for index in range(min(num_laps, 100)):
        pos = offset + 7 + index * 14
        laps.append({
            "lap_number": index + 1, "lap_time_ms": struct.unpack_from("<I", data, pos)[0],
            "sector1_ms": struct.unpack_from("<H", data, pos + 4)[0] + data[pos + 6] * 60_000,
            "sector2_ms": struct.unpack_from("<H", data, pos + 7)[0] + data[pos + 9] * 60_000,
            "sector3_ms": struct.unpack_from("<H", data, pos + 10)[0] + data[pos + 12] * 60_000,
            "valid_flags": data[pos + 13],
        })
    stints = []
    for index in range(min(num_stints, 8)):
        pos = offset + 7 + 1400 + index * 3
        stints.append({"end_lap": data[pos], "actual_compound": data[pos + 1], "visual_compound": data[pos + 2]})
    return {"car_index": car_index, "laps": laps, "stints": stints, "best_lap_number": data[offset + 3]}


def decode_tyre_sets(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_TYRE_SETS or len(data) < 231:
        return None
    car_index = data[HEADER_SIZE]
    sets = []
    for index in range(20):
        offset = HEADER_SIZE + 1 + index * 10
        sets.append({
            "index": index, "actual_compound": data[offset], "visual_compound": data[offset + 1],
            "wear_pct": data[offset + 2], "available": bool(data[offset + 3]),
            "recommended_session": data[offset + 4], "life_span_laps": data[offset + 5],
            "usable_life_laps": data[offset + 6], "lap_delta_ms": struct.unpack_from("<h", data, offset + 7)[0],
            "fitted": bool(data[offset + 9]),
        })
    return {"car_index": car_index, "sets": sets, "fitted_index": data[HEADER_SIZE + 201]}


def decode_motion_ex(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_MOTION_EX or len(data) < 273:
        return None
    values = struct.unpack_from("<61f", data, HEADER_SIZE)
    return {
        "suspension_position": list(values[0:4]), "wheel_speed": list(values[12:16]),
        "wheel_slip_ratio": list(values[16:20]), "wheel_slip_angle": list(values[20:24]),
        "local_velocity": list(values[33:36]), "angular_velocity": list(values[36:39]),
        "front_wheels_angle": values[42], "chassis_yaw": values[51], "chassis_pitch": values[52],
        "wheel_camber": list(values[53:57]), "wheel_camber_gain": list(values[57:61]),
    }


def decode_lap_positions(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    if not supported_header(header) or header.packet_id != PACKET_LAP_POSITIONS or len(data) < 1131:
        return None
    count, start = data[HEADER_SIZE], data[HEADER_SIZE + 1]
    rows = []
    for lap_offset in range(min(count, 50)):
        pos = HEADER_SIZE + 2 + lap_offset * MAX_CARS
        rows.append({"lap_index": start + lap_offset, "positions": list(data[pos:pos + MAX_CARS])})
    return {"lap_start": start, "laps": rows}


def decode_packet(data: bytes, header: PacketHeader) -> dict[str, Any] | None:
    """Decode relevant v1 packet data for durable session storage."""
    if not supported_header(header):
        return None
    decoders = {
        PACKET_MOTION: decode_motion, PACKET_SESSION: decode_session,
        PACKET_LAP_DATA: decode_all_lap_data, PACKET_EVENT: decode_event,
        PACKET_PARTICIPANTS: decode_participants, PACKET_CAR_STATUS: decode_car_status,
        PACKET_FINAL_CLASSIFICATION: decode_final_classification,
        PACKET_CAR_DAMAGE: decode_car_damage, PACKET_SESSION_HISTORY: decode_session_history,
        PACKET_TYRE_SETS: decode_tyre_sets, PACKET_MOTION_EX: decode_motion_ex,
        PACKET_LAP_POSITIONS: decode_lap_positions,
    }
    decoder = decoders.get(header.packet_id)
    if decoder is not None:
        return decoder(data, header)
    if header.packet_id == PACKET_CAR_TELEMETRY:
        player = decode_player_car_telemetry(data, header)
        return {"player": player} if player else None
    if header.packet_id == PACKET_CAR_SETUPS:
        player = decode_player_setup(data, header)
        return {"player": player} if player else None
    return None
