"""Distance alignment and evidence-based Silverstone comparison notes."""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

from .receiver import Lap

TRACE_FIELDS = ("speed_kph", "throttle", "brake", "steering", "gear", "current_lap_ms")
SILVERSTONE_WINDOWS = (
    ("Abbey–Farm", 0.00, 0.13),
    ("Village–Loop", 0.13, 0.25),
    ("Luffield–Woodcote", 0.32, 0.46),
    ("Copse", 0.46, 0.57),
    ("Maggotts–Becketts–Chapel", 0.57, 0.72),
    ("Hangar Straight", 0.72, 0.82),
    ("Stowe–Vale–Club", 0.82, 1.00),
)


class ComparisonError(ValueError):
    pass


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


def _markers(trace: list[dict[str, Any]]) -> dict[str, list[float]]:
    markers: dict[str, list[float]] = {"braking_starts_pct": [], "full_throttle_pct": []}
    for index in range(1, len(trace)):
        before, row = trace[index - 1], trace[index]
        if before.get("brake") is not None and row.get("brake") is not None and before["brake"] < 10 <= row["brake"]:
            markers["braking_starts_pct"].append(row["distance_pct"])
        if before.get("throttle") is not None and row.get("throttle") is not None and before["throttle"] < 95 <= row["throttle"]:
            markers["full_throttle_pct"].append(row["distance_pct"])
    return markers


def _window_notes(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], track_length: float) -> list[dict[str, Any]]:
    notes = []
    for name, start_fraction, end_fraction in SILVERSTONE_WINDOWS:
        start = round(start_fraction * (len(baseline) - 1))
        end = round(end_fraction * (len(baseline) - 1))
        b0, b1 = baseline[start], baseline[end]
        c0, c1 = candidate[start], candidate[end]
        window_delta = ((c1["current_lap_ms"] - c0["current_lap_ms"]) - (b1["current_lap_ms"] - b0["current_lap_ms"])) / 1000
        b_brake, c_brake = _crossing(baseline, "brake", 10, start, end), _crossing(candidate, "brake", 10, start, end)
        b_full, c_full = _crossing(baseline, "throttle", 95, start, end), _crossing(candidate, "throttle", 95, start, end)
        b_speeds = [row["speed_kph"] for row in baseline[start:end + 1] if row["speed_kph"] is not None]
        c_speeds = [row["speed_kph"] for row in candidate[start:end + 1] if row["speed_kph"] is not None]
        evidence: dict[str, Any] = {
            "window_delta_s": round(window_delta, 3),
            "baseline_min_speed_kph": round(min(b_speeds), 1) if b_speeds else None,
            "candidate_min_speed_kph": round(min(c_speeds), 1) if c_speeds else None,
        }
        statements = [f"Candidate {'gains' if window_delta < 0 else 'loses'} {abs(window_delta):.3f} s through this window."]
        if b_brake is not None and c_brake is not None:
            metres = (c_brake - b_brake) / 100 * track_length
            evidence["braking_shift_m"] = round(metres)
            statements.append(f"Candidate braking begins about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'} (10% threshold).")
        if b_full is not None and c_full is not None:
            metres = (c_full - b_full) / 100 * track_length
            evidence["full_throttle_shift_m"] = round(metres)
            statements.append(f"Candidate reaches 95% throttle about {abs(metres):.0f} m {'later' if metres > 0 else 'earlier'}.")
        if b_speeds and c_speeds:
            difference = min(c_speeds) - min(b_speeds)
            statements.append(f"Minimum speed is {abs(difference):.1f} km/h {'higher' if difference >= 0 else 'lower'} ({min(c_speeds):.1f} vs {min(b_speeds):.1f}).")
            if name == "Hangar Straight":
                top_difference = max(c_speeds) - max(b_speeds)
                evidence.update({"baseline_top_speed_kph": round(max(b_speeds), 1), "candidate_top_speed_kph": round(max(c_speeds), 1)})
                statements.append(f"Top speed is {abs(top_difference):.1f} km/h {'higher' if top_difference >= 0 else 'lower'} ({max(c_speeds):.1f} vs {max(b_speeds):.1f}).")
        notes.append({"name": name, "start_pct": start_fraction * 100, "end_pct": end_fraction * 100, "evidence": evidence, "statements": statements})
    return notes


def compare_laps(baseline: Lap, candidate: Lap) -> dict[str, Any]:
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
        if baseline.mode == "race" and baseline.recording_session_id != candidate.recording_session_id:
            raise ComparisonError("Race laps can only be compared within the same recording session")
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
    is_silverstone = baseline.track_id == candidate.track_id == 7
    if not has_real_distance:
        calibration = "Legacy approximation: lap distance was not recorded. Corner windows and metre-based engineer notes are disabled."
        windows = []
    elif not is_silverstone:
        calibration = "Silverstone engineer notes are unavailable because this track is not positively identified as normal Silverstone (track ID 7)."
        windows = []
    else:
        calibration = "Approximate normalized Silverstone windows; precise track-distance calibration is not yet implemented."
        windows = _window_notes(base_trace, candidate_trace, float(baseline.track_length_m or 5891))
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
        },
        "markers": {"baseline": _markers(base_trace), "candidate": _markers(candidate_trace)},
        "trace": trace,
        "engineer_notes": {
            "calibration": calibration,
            "windows": windows,
        },
    }
