"""Distance alignment and evidence-based Silverstone comparison notes."""

from __future__ import annotations

from bisect import bisect_right
from math import atan2, pi
from typing import Any

from .circuit_profiles import SILVERSTONE_SEED
from .receiver import Lap

TRACE_FIELDS = (
    "speed_kph", "throttle", "brake", "steering", "gear", "current_lap_ms",
    "world_x", "world_z", "front_wheels_angle", "chassis_yaw",
    "front_wheel_slip_ratio", "rear_wheel_slip_ratio", "front_wheel_slip_angle", "rear_wheel_slip_angle",
)


class ComparisonError(ValueError):
    pass


def calibration_trace(lap: Lap, max_points: int = 600) -> dict[str, Any]:
    """Return an editable X/Z distance trace and cautious curvature suggestions."""
    rows = [
        row for row in lap.samples
        if all(isinstance(row.get(key), (int, float)) for key in ("lap_distance_m", "world_x", "world_z"))
    ]
    reasons = []
    if lap.invalid:
        reasons.append("lap is invalid")
    if lap.events:
        reasons.append("lap contains a flashback or restart branch")
    if any(row.get("pit_status") not in (None, 0) for row in lap.samples):
        reasons.append("lap contains pit-lane samples")
    if len(rows) < 30:
        reasons.append("fewer than 30 motion samples")
    rows.sort(key=lambda row: float(row["lap_distance_m"]))
    if rows and lap.track_length_m and float(rows[-1]["lap_distance_m"]) - float(rows[0]["lap_distance_m"]) < lap.track_length_m * 0.85:
        reasons.append("distance coverage is below 85%")
    stride = max(1, len(rows) // max_points)
    raw_points = [{
        "distance_m": round(float(row["lap_distance_m"]), 2),
        "x": round(float(row["world_x"]), 3), "z": round(float(row["world_z"]), 3),
    } for row in rows[::stride]]
    points = []
    for index, point in enumerate(raw_points):
        # A centered five-point mean suppresses coordinate jitter without
        # shifting distance or aggressively reshaping genuine corners.
        window = raw_points[max(0, index - 2):min(len(raw_points), index + 3)]
        points.append({
            "distance_m": point["distance_m"],
            "x": round(sum(item["x"] for item in window) / len(window), 3),
            "z": round(sum(item["z"] for item in window) / len(window), 3),
        })
    candidates = []
    # Heading change across a deliberately broad window suppresses UDP jitter.
    for index in range(3, len(points) - 3):
        before, centre, after = points[index - 3], points[index], points[index + 3]
        first = atan2(centre["z"] - before["z"], centre["x"] - before["x"])
        second = atan2(after["z"] - centre["z"], after["x"] - centre["x"])
        change = (second - first + pi) % (2 * pi) - pi
        if abs(change) >= 0.12 and (not candidates or centre["distance_m"] - candidates[-1]["distance_m"] >= 80):
            candidates.append({
                "distance_m": centre["distance_m"], "direction": "left" if change > 0 else "right",
                "heading_change_deg": round(change * 180 / pi, 1), "status": "suggested_unverified",
            })
    return {
        "lap_id": lap.id, "eligible_for_profile_calibration": not reasons,
        "exclusion_reasons": reasons, "points": points, "turn_candidates": candidates,
        "notice": "Geometry is a lightly smoothed game-coordinate trace (centered five-point mean). Suggested turns require manual verification.",
    }


def _source(lap: Lap) -> tuple[list[dict[str, Any]], str]:
    distance_rows = [
        row for row in lap.samples
        if isinstance(row.get("lap_distance_m"), (int, float)) and row["lap_distance_m"] >= 0
    ]
    if len(distance_rows) >= 2:
        maximum = max(float(row["lap_distance_m"]) for row in distance_rows)
        if maximum > 100:
            return [dict(row, _x=float(row["lap_distance_m"]) / maximum) for row in distance_rows], "distance"
    timed = [row for row in lap.samples if isinstance(row.get("t"), (int, float))]
    if len(timed) < 2:
        raise ComparisonError(f"Lap {lap.id} has insufficient samples")
    start, end = float(timed[0]["t"]), float(timed[-1]["t"])
    duration = max(end - start, 0.001)
    return [dict(row, _x=(float(row["t"]) - start) / duration) for row in timed], "legacy-time"


def _interpolate(rows: list[dict[str, Any]], xs: list[float], x: float, field: str, lap_time_ms: int) -> float | None:
    right = min(max(bisect_right(xs, x), 1), len(rows) - 1)
    left = right - 1
    a, b = rows[left], rows[right]
    av, bv = a.get(field), b.get(field)
    if field == "current_lap_ms" and (av is None or bv is None):
        return x * lap_time_ms
    if not isinstance(av, (int, float)) or not isinstance(bv, (int, float)):
        return None
    span = b["_x"] - a["_x"]
    ratio = 0 if span <= 0 else (x - a["_x"]) / span
    return float(av) + (float(bv) - float(av)) * ratio


def aligned_trace(lap: Lap, points: int = 501) -> tuple[list[dict[str, Any]], str]:
    rows, quality = _source(lap)
    rows.sort(key=lambda row: row["_x"])
    xs = [row["_x"] for row in rows]
    axis = [index / (points - 1) for index in range(points)]
    trace = [{
        "distance_pct": round(x * 100, 2),
        **{field: _interpolate(rows, xs, x, field, lap.time_ms) for field in TRACE_FIELDS},
    } for x in axis]
    # UDP sampling normally stops just before the timing line. Distribute the
    # small endpoint correction across the lap instead of creating a final spike.
    raw_start = trace[0]["current_lap_ms"] or 0
    raw_end = trace[-1]["current_lap_ms"] or lap.time_ms
    correction = lap.time_ms - (raw_end - raw_start)
    for index, row in enumerate(trace):
        progress = index / (points - 1)
        row["current_lap_ms"] = (row["current_lap_ms"] or 0) - raw_start + progress * correction
    return trace, quality


def _endpoint_adjustment(lap: Lap) -> float:
    timed = [row.get("current_lap_ms") for row in lap.samples if isinstance(row.get("current_lap_ms"), (int, float))]
    if len(timed) < 2:
        return 0.0
    return float(lap.time_ms) - (float(timed[-1]) - float(timed[0]))


def _crossing(trace: list[dict[str, Any]], field: str, threshold: float, start: int, end: int) -> float | None:
    previous = trace[max(0, start - 1)].get(field)
    for row in trace[start:end + 1]:
        value = row.get(field)
        if value is not None and previous is not None and previous < threshold <= value:
            return float(row["distance_pct"])
        previous = value
    return None


def _falling(trace: list[dict[str, Any]], field: str, threshold: float, start: int, end: int) -> float | None:
    previous = trace[max(0, start - 1)].get(field)
    for row in trace[start:end + 1]:
        value = row.get(field)
        if value is not None and previous is not None and previous >= threshold > value:
            return float(row["distance_pct"])
        previous = value
    return None


def _absolute_crossing(trace: list[dict[str, Any]], field: str, threshold: float, start: int, end: int) -> float | None:
    previous = trace[max(0, start - 1)].get(field)
    for row in trace[start:end + 1]:
        value = row.get(field)
        if value is not None and previous is not None and abs(previous) < threshold <= abs(value):
            return float(row["distance_pct"])
        previous = value
    return None


def _markers(trace: list[dict[str, Any]]) -> dict[str, list[float]]:
    markers: dict[str, list[float]] = {"braking_starts_pct": [], "full_throttle_pct": []}
    for index in range(1, len(trace)):
        before, row = trace[index - 1], trace[index]
        if before.get("brake") is not None and row.get("brake") is not None and before["brake"] < 10 <= row["brake"]:
            markers["braking_starts_pct"].append(row["distance_pct"])
        if before.get("throttle") is not None and row.get("throttle") is not None and before["throttle"] < 95 <= row["throttle"]:
            markers["full_throttle_pct"].append(row["distance_pct"])
    return markers


def _context_flags(lap: Lap) -> list[str]:
    context = lap.race_context or {}
    flags: list[str] = []
    for phase in ("start", "finish"):
        snapshot = context.get(phase, {}) if isinstance(context, dict) else {}
        lap_state = snapshot.get("lap", {})
        session = snapshot.get("session", {})
        damage = snapshot.get("damage", {})
        if lap_state.get("pit_status") not in (None, 0):
            flags.append("pit lane affected")
        if session.get("safety_car_status") not in (None, 0):
            flags.append("safety car/VSC affected")
        if isinstance(lap_state.get("delta_to_car_in_front_ms"), (int, float)) and 0 < lap_state["delta_to_car_in_front_ms"] < 2_000:
            flags.append("traffic within 2 seconds")
        damage_values = [value for key, value in damage.items() if key.endswith("damage_pct") and isinstance(value, (int, float))]
        if any(value > 0 for value in damage_values):
            flags.append("car damage recorded")
    return list(dict.fromkeys(flags))


def _window_notes(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]],
    track_length: float, turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    notes = []
    for turn in turns:
        name = turn.get("name") or f"Turn {turn.get('number', '?')}"
        start_fraction = max(0.0, min(1.0, float(turn["entry_m"]) / track_length))
        end_fraction = max(start_fraction, min(1.0, float(turn["exit_m"]) / track_length))
        apex_fraction = max(start_fraction, min(end_fraction, float(turn.get("estimated_apex_m", turn["entry_m"])) / track_length))
        start = round(start_fraction * (len(baseline) - 1))
        end = round(end_fraction * (len(baseline) - 1))
        apex = round(apex_fraction * (len(baseline) - 1))
        b0, b1 = baseline[start], baseline[end]
        c0, c1 = candidate[start], candidate[end]
        window_delta = ((c1["current_lap_ms"] - c0["current_lap_ms"]) - (b1["current_lap_ms"] - b0["current_lap_ms"])) / 1000
        b_brake, c_brake = _crossing(baseline, "brake", 10, start, end), _crossing(candidate, "brake", 10, start, end)
        b_release, c_release = _falling(baseline, "brake", 10, start, end), _falling(candidate, "brake", 10, start, end)
        b_turn_in, c_turn_in = _absolute_crossing(baseline, "steering", 5, start, end), _absolute_crossing(candidate, "steering", 5, start, end)
        b_pickup, c_pickup = _crossing(baseline, "throttle", 20, start, end), _crossing(candidate, "throttle", 20, start, end)
        b_full, c_full = _crossing(baseline, "throttle", 95, start, end), _crossing(candidate, "throttle", 95, start, end)
        b_speeds = [row["speed_kph"] for row in baseline[start:end + 1] if row["speed_kph"] is not None]
        c_speeds = [row["speed_kph"] for row in candidate[start:end + 1] if row["speed_kph"] is not None]
        evidence: dict[str, Any] = {
            "window_delta_s": round(window_delta, 3),
            "baseline_min_speed_kph": round(min(b_speeds), 1) if b_speeds else None,
            "candidate_min_speed_kph": round(min(c_speeds), 1) if c_speeds else None,
            "estimated_apex_m": round(apex_fraction * track_length, 1),
            "baseline_apex_speed_kph": baseline[apex].get("speed_kph"),
            "candidate_apex_speed_kph": candidate[apex].get("speed_kph"),
            "baseline_exit_speed_kph": baseline[end].get("speed_kph"),
            "candidate_exit_speed_kph": candidate[end].get("speed_kph"),
            "baseline_brake_release_m": round(b_release / 100 * track_length, 1) if b_release is not None else None,
            "candidate_brake_release_m": round(c_release / 100 * track_length, 1) if c_release is not None else None,
            "baseline_turn_in_m": round(b_turn_in / 100 * track_length, 1) if b_turn_in is not None else None,
            "candidate_turn_in_m": round(c_turn_in / 100 * track_length, 1) if c_turn_in is not None else None,
            "baseline_throttle_pickup_m": round(b_pickup / 100 * track_length, 1) if b_pickup is not None else None,
            "candidate_throttle_pickup_m": round(c_pickup / 100 * track_length, 1) if c_pickup is not None else None,
            "entry_delta_s": round((c0["current_lap_ms"] - b0["current_lap_ms"]) / 1000, 3),
            "estimated_apex_delta_s": round((candidate[apex]["current_lap_ms"] - baseline[apex]["current_lap_ms"]) / 1000, 3),
            "exit_delta_s": round((c1["current_lap_ms"] - b1["current_lap_ms"]) / 1000, 3),
        }
        statements = [f"Candidate {'gains' if window_delta < 0 else 'loses'} {abs(window_delta):.3f} s through this window."]
        if b_brake is not None and c_brake is not None:
            metres = (c_brake - b_brake) / 100 * track_length
            evidence["braking_shift_m"] = round(metres)
            statements.append(f"Candidate braking begins about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'} (10% threshold).")
        if b_release is not None and c_release is not None:
            metres = (c_release - b_release) / 100 * track_length
            statements.append(f"Candidate releases below 10% brake about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'}.")
        if b_turn_in is not None and c_turn_in is not None:
            metres = (c_turn_in - b_turn_in) / 100 * track_length
            statements.append(f"Candidate crosses the 5% steering turn-in threshold about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'}.")
        if b_pickup is not None and c_pickup is not None:
            metres = (c_pickup - b_pickup) / 100 * track_length
            statements.append(f"Candidate crosses 20% throttle pickup about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'}.")
        if b_full is not None and c_full is not None:
            metres = (c_full - b_full) / 100 * track_length
            evidence["full_throttle_shift_m"] = round(metres)
            statements.append(f"Candidate reaches 95% throttle about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'}.")
        if b_speeds and c_speeds:
            difference = min(c_speeds) - min(b_speeds)
            statements.append(f"Minimum speed is {abs(difference):.1f} km/h {'higher' if difference >= 0 else 'lower'} ({min(c_speeds):.1f} vs {min(b_speeds):.1f}).")
            apex_base, apex_candidate = baseline[apex].get("speed_kph"), candidate[apex].get("speed_kph")
            if apex_base is not None and apex_candidate is not None:
                statements.append(f"Speed at the profile's estimated apex is {apex_candidate:.1f} vs {apex_base:.1f} km/h; this apex position is profile-derived, not detected from the car.")
            exit_base, exit_candidate = baseline[end].get("speed_kph"), candidate[end].get("speed_kph")
            if exit_base is not None and exit_candidate is not None:
                statements.append(f"Exit-boundary speed is {exit_candidate:.1f} vs {exit_base:.1f} km/h.")
            if turn.get("direction") == "straight":
                top_difference = max(c_speeds) - max(b_speeds)
                evidence.update({"baseline_top_speed_kph": round(max(b_speeds), 1), "candidate_top_speed_kph": round(max(c_speeds), 1)})
                statements.append(f"Top speed is {abs(top_difference):.1f} km/h {'higher' if top_difference >= 0 else 'lower'} ({max(c_speeds):.1f} vs {max(b_speeds):.1f}).")
        notes.append({
            "number": turn.get("number"), "name": name, "direction": turn.get("direction", "unknown"),
            "linked_group": turn.get("linked_group"), "verification_status": turn.get("verification_status", "unverified"),
            "start_pct": start_fraction * 100, "end_pct": end_fraction * 100,
            "entry_m": turn["entry_m"], "estimated_apex_m": turn.get("estimated_apex_m"), "exit_m": turn["exit_m"],
            "evidence": evidence, "statements": statements,
        })
    return notes


def compare_laps(baseline: Lap, candidate: Lap, circuit_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    if baseline.invalid or candidate.invalid:
        raise ComparisonError("Only valid laps can be compared")
    both_legacy = baseline.recording_session_id == candidate.recording_session_id == "legacy"
    if not both_legacy:
        if baseline.mode != candidate.mode:
            if {baseline.mode, candidate.mode} == {"race", "time_trial"}:
                raise ComparisonError("Race and Time Trial laps cannot be compared")
            raise ComparisonError("Legacy or unknown-mode laps cannot be compared with recorded sessions")
        if baseline.track_id is None or candidate.track_id is None or baseline.track_id < 0 or candidate.track_id < 0:
            raise ComparisonError("Both laps need a known track before they can be compared")
        if baseline.track_id != candidate.track_id:
            raise ComparisonError("Laps from different tracks cannot be compared")
        if (
            baseline.mode == "race"
            and baseline.recording_session_id != candidate.recording_session_id
            and (circuit_profile is None or circuit_profile.get("verification_status") != "verified")
        ):
            raise ComparisonError("Cross-session race comparison requires the same manually verified circuit profile")
        if baseline.mode not in {"race", "time_trial"} and baseline.recording_session_id != candidate.recording_session_id:
            raise ComparisonError("These laps do not have compatible recording modes")
    base_trace, base_quality = aligned_trace(baseline)
    candidate_trace, candidate_quality = aligned_trace(candidate)
    if (
        baseline.mode == candidate.mode == "time_trial"
        and baseline.recording_session_id != candidate.recording_session_id
        and base_quality != candidate_quality
    ):
        raise ComparisonError("Time Trial runs use incompatible alignment data")
    trace, deltas = [], []
    for base, cand in zip(base_trace, candidate_trace):
        delta = (cand["current_lap_ms"] - base["current_lap_ms"]) / 1000
        deltas.append(delta)
        trace.append({
            "distance_pct": base["distance_pct"],
            "baseline": {key: base[key] for key in TRACE_FIELDS},
            "candidate": {key: cand[key] for key in TRACE_FIELDS},
            "delta_s": round(delta, 4),
        })
    final_delta = (candidate.time_ms - baseline.time_ms) / 1000
    has_real_distance = base_quality == candidate_quality == "distance"
    if circuit_profile is None and baseline.track_id == candidate.track_id == 7:
        circuit_profile = SILVERSTONE_SEED
    if not has_real_distance:
        calibration = "Legacy approximation: lap distance was not recorded. Corner windows and metre-based engineer notes are disabled."
        windows = []
    elif circuit_profile is None or not circuit_profile.get("turns"):
        calibration = "No calibrated turn profile is selected. Full-lap traces remain available; add turn boundaries in Circuit Profiles for turn analysis."
        windows = []
    else:
        status = circuit_profile.get("verification_status", "unverified")
        calibration = f"Circuit profile {circuit_profile.get('circuit_name', circuit_profile.get('id'))} is {status}; apex values are estimates unless manually verified."
        windows = _window_notes(
            base_trace, candidate_trace,
            float(circuit_profile.get("measured_game_length_m") or baseline.track_length_m or 1),
            circuit_profile["turns"],
        )
    return {
        "baseline": baseline.summary(), "candidate": candidate.summary(),
        "alignment": {"baseline": base_quality, "candidate": candidate_quality, "points": len(trace)},
        "endpoint_estimation": {
            "method": "The gap from the last UDP sample to each official lap time is distributed linearly over the trace.",
            "baseline_adjustment_ms": round(_endpoint_adjustment(baseline), 1),
            "candidate_adjustment_ms": round(_endpoint_adjustment(candidate), 1),
        },
        "delta_definition": "candidate time minus baseline time; negative means candidate is faster",
        "summary": {
            "final_delta_s": round(final_delta, 3), "maximum_gain_s": round(min(deltas), 3),
            "maximum_loss_s": round(max(deltas), 3), "baseline_peak_speed_kph": baseline.peak_speed_kph,
            "candidate_peak_speed_kph": candidate.peak_speed_kph, "baseline_minimum_speed_kph": baseline.minimum_speed_kph,
            "candidate_minimum_speed_kph": candidate.minimum_speed_kph,
            "sector_deltas_s": [
                round(((candidate.sector1_ms or 0) - (baseline.sector1_ms or 0)) / 1000, 3) if baseline.sector1_ms and candidate.sector1_ms else None,
                round(((candidate.sector2_ms or 0) - (baseline.sector2_ms or 0)) / 1000, 3) if baseline.sector2_ms and candidate.sector2_ms else None,
                round(
                    ((candidate.time_ms - (candidate.sector1_ms or 0) - (candidate.sector2_ms or 0)) -
                     (baseline.time_ms - (baseline.sector1_ms or 0) - (baseline.sector2_ms or 0))) / 1000, 3,
                ) if all((baseline.sector1_ms, baseline.sector2_ms, candidate.sector1_ms, candidate.sector2_ms)) else None,
            ],
        },
        "markers": {"baseline": _markers(base_trace), "candidate": _markers(candidate_trace)},
        "race_context": {
            "baseline_flags": _context_flags(baseline), "candidate_flags": _context_flags(candidate),
            "notice": "Race context can explain lap-time differences; these deltas are not automatically attributed to driver pace.",
        },
        "trace": trace,
        "engineer_notes": {
            "calibration": calibration,
            "profile": ({key: circuit_profile.get(key) for key in ("id", "circuit_name", "layout", "verification_status", "provenance")} if circuit_profile else None),
            "windows": windows,
        },
    }
