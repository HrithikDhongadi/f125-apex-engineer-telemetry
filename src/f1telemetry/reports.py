"""Read-only Markdown and full-fidelity ZIP exports."""

from __future__ import annotations

from dataclasses import asdict
from collections import Counter
from io import BytesIO
import json
import re
from statistics import mean, pstdev
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .analysis import ComparisonError, compare_laps
from .circuit_profiles import profile_audit
from .quality import lap_quality
from .setups import setup_rows


class ExportError(ValueError):
    pass


def safe_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return clean[:80] or "apex-engineer-session"


def _time(ms: int | None) -> str:
    if ms is None:
        return "unavailable"
    return f"{ms // 60_000}:{(ms % 60_000) / 1000:06.3f}"


def _setup(setup: dict[str, Any] | None) -> str:
    parts = []
    for row in setup_rows(setup):
        value = "unknown" if row["value"] is None else f"{row['value']}{(' ' + row['unit']) if row['unit'] else ''}"
        source = "manual" if row["source"] == "manual" else "UDP" if row["source"] else "missing"
        parts.append(f"{row['label']}: {value} [{source}]")
    return "; ".join(parts)


def _observations(lap: Any) -> list[str]:
    speeds = [float(row["speed_kph"]) for row in lap.samples if isinstance(row.get("speed_kph"), (int, float))]
    brakes = [float(row["brake"]) for row in lap.samples if isinstance(row.get("brake"), (int, float))]
    throttles = [float(row["throttle"]) for row in lap.samples if isinstance(row.get("throttle"), (int, float))]
    observations = []
    if speeds:
        observations.append(f"Recorded speed range: {min(speeds):.0f}–{max(speeds):.0f} km/h.")
    if brakes:
        observations.append(f"Brake input ≥10% in {sum(value >= 10 for value in brakes) / len(brakes) * 100:.1f}% of samples.")
    if throttles:
        observations.append(f"Throttle ≥95% in {sum(value >= 95 for value in throttles) / len(throttles) * 100:.1f}% of samples.")
    return observations or ["Telemetry observations unavailable."]


WHEELS = ("rear left", "rear right", "front left", "front right")
ACTUAL_COMPOUNDS = {7: "intermediate", 8: "wet", 16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1", 21: "C0"}
VISUAL_COMPOUNDS = {7: "intermediate", 8: "wet", 16: "soft", 17: "medium", 18: "hard"}


def _context(lap: Any, phase: str, group: str) -> dict[str, Any]:
    context = lap.race_context if isinstance(lap.race_context, dict) else {}
    snapshot = context.get(phase, {}) if isinstance(context.get(phase, {}), dict) else {}
    value = snapshot.get(group, {})
    return value if isinstance(value, dict) else {}


def _number(value: Any, decimals: int = 1, suffix: str = "") -> str:
    if not isinstance(value, (int, float)):
        return "unavailable"
    return f"{float(value):.{decimals}f}{suffix}"


def _wheel_text(values: Any, decimals: int = 1, suffix: str = "") -> str:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return "unavailable"
    return ", ".join(f"{name} {_number(value, decimals, suffix)}" for name, value in zip(WHEELS, values))


def _wheel_stat(lap: Any, field: str, operation: str) -> list[float] | None:
    wheels: list[list[float]] = [[], [], [], []]
    for sample in lap.samples:
        values = sample.get(field)
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            continue
        for index, value in enumerate(values):
            if isinstance(value, (int, float)):
                wheels[index].append(float(value))
    if not all(wheels):
        return None
    return [max(values) if operation == "max" else min(values) if operation == "min" else mean(values) for values in wheels]


def _compound(status: dict[str, Any]) -> str:
    actual, visual = status.get("actual_tyre_compound"), status.get("visual_tyre_compound")
    actual_label = ACTUAL_COMPOUNDS.get(actual, f"ID {actual}" if actual is not None else "unknown")
    visual_label = VISUAL_COMPOUNDS.get(visual, f"ID {visual}" if visual is not None else "unknown")
    return f"{visual_label} / {actual_label}"


def _change(start: Any, finish: Any, decimals: int = 1, suffix: str = "") -> str:
    if not isinstance(start, (int, float)) or not isinstance(finish, (int, float)):
        return "unavailable"
    delta = float(finish) - float(start)
    return f"{_number(start, decimals, suffix)} → {_number(finish, decimals, suffix)} ({delta:+.{decimals}f}{suffix})"


def _event_description(event: dict[str, Any]) -> str:
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    player = event.get("player_car_index")
    event_type = event.get("type", "event")
    if event_type == "overtake" and player is not None:
        if details.get("overtaking_vehicle_index") == player:
            return f"player passed car index {details.get('overtaken_vehicle_index', '?')}"
        if details.get("overtaken_vehicle_index") == player:
            return f"player was passed by car index {details.get('overtaking_vehicle_index', '?')}"
    if event_type == "collision" and player is not None:
        other = details.get("vehicle_2_index") if details.get("vehicle_1_index") == player else details.get("vehicle_1_index")
        return f"player-involved collision with car index {other if other is not None else '?'}"
    if event_type == "penalty" and details.get("vehicle_index") == player:
        return (
            f"player {event.get('penalty_label', 'penalty-related event')} "
            f"({event.get('infringement_label', 'unknown infringement')})"
        )
    return str(event_type).replace("_", " ")


def _lap_engineering_detail(
    lap: Any, note: str, timeline: list[dict[str, Any]],
    setup_audit: dict[str, Any] | None = None,
) -> list[str]:
    quality = lap_quality(lap)
    start_lap, finish_lap = _context(lap, "start", "lap"), _context(lap, "finish", "lap")
    start_status, finish_status = _context(lap, "start", "car_status"), _context(lap, "finish", "car_status")
    start_damage, finish_damage = _context(lap, "start", "damage"), _context(lap, "finish", "damage")
    start_session, finish_session = _context(lap, "start", "session"), _context(lap, "finish", "session")
    wear_start, wear_finish = start_damage.get("tyre_wear_pct"), finish_damage.get("tyre_wear_pct")
    wear_delta = None
    if isinstance(wear_start, list) and isinstance(wear_finish, list) and len(wear_start) == len(wear_finish) == 4:
        wear_delta = [float(end) - float(begin) for begin, end in zip(wear_start, wear_finish)]
    fuel_start, fuel_finish = start_status.get("fuel_kg"), finish_status.get("fuel_kg")
    fuel_used = float(fuel_start) - float(fuel_finish) if isinstance(fuel_start, (int, float)) and isinstance(fuel_finish, (int, float)) else None
    event_rows = [
        event for event in timeline
        if event.get("lap_number") == lap.number
        and event.get("event_validity", "accepted") == "accepted"
        and event.get("type") not in {"controller_input", "butn", "speed_trap", "damage_change"}
        and event.get("player_relevant") is not False
    ]
    scalar_damage = (
        "front_left_wing_damage_pct", "front_right_wing_damage_pct", "rear_wing_damage_pct",
        "floor_damage_pct", "diffuser_damage_pct", "sidepod_damage_pct", "gearbox_damage_pct", "engine_damage_pct",
    )
    damage_changes = [
        f"{key.replace('_pct', '').replace('_', ' ')} {_change(start_damage.get(key), finish_damage.get(key), 0, '%')}"
        for key in scalar_damage
        if start_damage.get(key) or finish_damage.get(key)
    ]
    coverage = f"{quality['coverage_pct']:.1f}%" if quality["coverage_pct"] is not None else "unavailable"
    lines = [
        f"### Lap {lap.number} — {_time(lap.time_ms)}{' — INVALID (game flag)' if lap.invalid else ''}", "",
        "#### Timing and data quality", "",
        f"- Official sectors: {_time(lap.sector1_ms)} / {_time(lap.sector2_ms)} / {_time(lap.time_ms - (lap.sector1_ms or 0) - (lap.sector2_ms or 0)) if lap.sector1_ms and lap.sector2_ms else 'unavailable'}.",
        f"- Samples: {len(lap.samples)}; alignment: {quality['alignment']}; circuit coverage: {coverage}; distance span: {_number(quality['distance_span_m'], 1, ' m')}.",
        f"- Speed: minimum {lap.minimum_speed_kph if lap.minimum_speed_kph is not None else 'unavailable'} km/h; peak {lap.peak_speed_kph if lap.peak_speed_kph is not None else 'unavailable'} km/h.",
        f"- Engineer note: {note or 'none'}.", "",
        "#### Race position and operating context", "",
        f"- Position: {start_lap.get('position', 'unavailable')} → {finish_lap.get('position', 'unavailable')}; grid position {start_lap.get('grid_position', 'unavailable')}.",
        f"- Recorded lap-start context: gap ahead {start_lap.get('delta_to_car_in_front_ms', 'unavailable')} ms; fuel {start_status.get('fuel_kg', 'unavailable')} kg.",
        f"- Gap ahead: {start_lap.get('delta_to_car_in_front_ms', 'unavailable')} → {finish_lap.get('delta_to_car_in_front_ms', 'unavailable')} ms; leader gap: {start_lap.get('delta_to_race_leader_ms', 'unavailable')} → {finish_lap.get('delta_to_race_leader_ms', 'unavailable')} ms.",
        f"- Penalties: {start_lap.get('penalties_s', 'unavailable')} → {finish_lap.get('penalties_s', 'unavailable')} s; warnings: {start_lap.get('warnings', 'unavailable')} → {finish_lap.get('warnings', 'unavailable')}.",
        f"- Pit status: {start_lap.get('pit_status', 'unavailable')} → {finish_lap.get('pit_status', 'unavailable')}; stops recorded: {finish_lap.get('pit_stops', start_lap.get('pit_stops', 'unavailable'))}.",
        f"- Weather ID: {start_session.get('weather', 'unavailable')} → {finish_session.get('weather', 'unavailable')}; safety-car state: {start_session.get('safety_car_status', 'unavailable')} → {finish_session.get('safety_car_status', 'unavailable')}; air/track temperature: {start_session.get('air_temperature_c', 'unavailable')} / {start_session.get('track_temperature_c', 'unavailable')} °C.", "",
        "#### Tyres, degradation and temperatures", "",
        f"- Compound at lap start: {_compound(start_status)}; tyre age {start_status.get('tyre_age_laps', 'unavailable')} laps.",
        f"- Wear at start: {_wheel_text(wear_start, 1, '%')}.",
        f"- Wear at finish: {_wheel_text(wear_finish, 1, '%')}.",
        f"- Wear added this lap: {_wheel_text(wear_delta, 2, ' percentage points')}.",
        f"- Measured inner temperature average: {_wheel_text(_wheel_stat(lap, 'tyre_inner_c', 'mean'), 1, ' °C')}.",
        f"- Measured inner temperature maximum: {_wheel_text(_wheel_stat(lap, 'tyre_inner_c', 'max'), 1, ' °C')}.",
        f"- Measured surface temperature average: {_wheel_text(_wheel_stat(lap, 'tyre_surface_c', 'mean'), 1, ' °C')}.",
        f"- Measured surface temperature maximum: {_wheel_text(_wheel_stat(lap, 'tyre_surface_c', 'max'), 1, ' °C')}.",
        f"- Measured running pressure average: {_wheel_text(_wheel_stat(lap, 'tyre_pressures_psi', 'mean'), 2, ' PSI')} (not configured setup pressure).",
        f"- Tyre damage at finish: {_wheel_text(finish_damage.get('tyre_damage_pct'), 0, '%')}; blisters: {_wheel_text(finish_damage.get('tyre_blisters_pct'), 0, '%')}.", "",
        "#### Fuel, ERS, brakes and car condition", "",
        f"- Fuel: {_change(fuel_start, fuel_finish, 2, ' kg')}; calculated net used {(_number(fuel_used, 2, ' kg') if fuel_used is not None else 'unavailable')}.",
        f"- Fuel remaining estimate: {_change(start_status.get('fuel_remaining_laps'), finish_status.get('fuel_remaining_laps'), 2, ' laps')}.",
        f"- ERS store: {_change(start_status.get('ers_store_j'), finish_status.get('ers_store_j'), 0, ' J')}; deployed-this-lap counter at finish {_number(finish_status.get('ers_deployed_this_lap_j'), 0, ' J')}.",
        f"- Maximum measured brake temperatures: {_wheel_text(_wheel_stat(lap, 'brake_temps_c', 'max'), 0, ' °C')}.",
        f"- Damage changes: {'; '.join(damage_changes) if damage_changes else 'no non-zero body/engine damage captured in the lap boundary snapshots'}.",
        f"- Faults at finish: DRS {finish_damage.get('drs_fault', 'unavailable')}; ERS {finish_damage.get('ers_fault', 'unavailable')}.", "",
        "#### Driver-input trace", "",
        *[f"- {item}" for item in _observations(lap)], "",
        "#### Setup snapshot", "",
        "- These are configured values from the game Car Setups packet. Configured starting fuel is not instantaneous remaining fuel; measured fuel is reported above.",
    ]
    selected_setup = (setup_audit or {}).get("selected_setup")
    recorded_setup = lap.setup or {}
    comparable = lambda setup: {key: value for key, value in (setup or {}).items() if not str(key).startswith("_")}
    setup_status = (
        "confirmed_active" if selected_setup and comparable(recorded_setup) == comparable(selected_setup)
        else "superseded" if selected_setup else "unknown"
    )
    lines.append(f"- Snapshot validation: **{setup_status}**.")
    if setup_status == "superseded":
        lines.append("- The values below are preserved from the lap file as raw historical evidence; the reconstructed active setup follows them.")
    for row in setup_rows(recorded_setup):
        value = "unknown" if row["value"] is None else f"{row['value']}{(' ' + row['unit']) if row['unit'] else ''}"
        source = "manually supplied" if row["source"] == "manual" else "decoded from UDP" if row["source"] else "unknown"
        lines.append(f"- {row['label']}: {value} — {source}")
    if setup_status == "superseded":
        lines.extend(["", "##### Reconstructed active setup", ""])
        lines.append(f"- Provenance: `{(setup_audit or {}).get('status', 'unknown')}`; {(setup_audit or {}).get('reason', 'no reason available')}.")
        for row in setup_rows(selected_setup):
            value = "unknown" if row["value"] is None else f"{row['value']}{(' ' + row['unit']) if row['unit'] else ''}"
            lines.append(f"- {row['label']}: {value} — decoded from UDP")
    lines.extend(["", "#### Relevant lap events", ""])
    if event_rows:
        lines.extend(f"- {event.get('session_time', 0):.3f} s · {_event_description(event)} · {event.get('source', 'unknown')}." for event in event_rows)
    else:
        lines.append("- None recorded (controller inputs and repetitive speed-trap messages excluded).")
    lines.append("")
    return lines


def _stint_lines(laps: list[Any]) -> list[str]:
    stints: list[list[Any]] = []
    for lap in sorted(laps, key=lambda item: item.number):
        status = _context(lap, "start", "car_status")
        if not stints:
            stints.append([lap])
            continue
        previous = _context(stints[-1][-1], "start", "car_status")
        compound_changed = status.get("actual_tyre_compound") != previous.get("actual_tyre_compound")
        age_reset = isinstance(status.get("tyre_age_laps"), int) and isinstance(previous.get("tyre_age_laps"), int) and status["tyre_age_laps"] < previous["tyre_age_laps"]
        (stints.append([lap]) if compound_changed or age_reset else stints[-1].append(lap))
    lines = ["## Stint analysis", ""]
    for index, stint in enumerate(stints, 1):
        valid_times = [lap.time_ms for lap in stint if not lap.invalid]
        first_status = _context(stint[0], "start", "car_status")
        first_wear = _context(stint[0], "start", "damage").get("tyre_wear_pct")
        last_wear = _context(stint[-1], "finish", "damage").get("tyre_wear_pct")
        lines.extend([
            f"### Stint {index} — laps {stint[0].number}–{stint[-1].number}", "",
            f"- Compound: {_compound(first_status)}; starting tyre age {first_status.get('tyre_age_laps', 'unavailable')} laps.",
            f"- Valid-lap average: {_time(round(mean(valid_times))) if valid_times else 'unavailable'}; fastest: {_time(min(valid_times)) if valid_times else 'unavailable'}.",
            f"- Wear: {_wheel_text(first_wear, 1, '%')} → {_wheel_text(last_wear, 1, '%')}.", "",
        ])
    return lines


def build_markdown(
    session: Any, laps: list[Any], notes: dict[str, str], track_label: str,
    timeline: list[dict[str, Any]] | None = None,
    storage_summary: dict[str, Any] | None = None,
    circuit_profile: dict[str, Any] | None = None, report_type: str = "auto",
) -> str:
    aliases = {"tt": "time_trial", "lap": "lap_analysis"}
    report_type = aliases.get(report_type, report_type)
    if report_type == "auto":
        report_type = "race" if session.mode == "race" else "time_trial" if session.mode == "time_trial" else "lap_analysis"
    if report_type not in {"race", "time_trial", "lap_analysis"}:
        raise ExportError("Report type must be auto, lap_analysis, race or time_trial")
    if report_type == "race" and session.mode != "race":
        raise ExportError("Race reports require a race session")
    if report_type == "time_trial" and session.mode != "time_trial":
        raise ExportError("Time Trial reports require a Time Trial session")
    title = {"race": "Race Report", "time_trial": "Time Trial Report", "lap_analysis": "Lap Analysis Report"}[report_type]
    lines = [
        f"# Apex Engineer {title} — {session.name}", "",
        "## Session", "",
        f"- Session ID: `{session.id}`", f"- Circuit: {track_label} (track ID {session.track_id if session.track_id is not None else 'unknown'})",
        f"- Game mode: {session.mode}", f"- Capture started: {session.started_at or 'unavailable'}",
        f"- Capture ended: {session.ended_at or 'unavailable'}", f"- Status: {session.status}",
        f"- Included laps: {', '.join(str(lap.number) for lap in laps) if laps else 'none'}", "",
        f"- Report type: {report_type}",
    ]
    if storage_summary:
        versions = storage_summary.get("processing_versions") or {}
        lines[10:10] = [
            f"- Continuous packets: {storage_summary.get('packet_count', 'unknown')}",
            f"- Timeline events: {storage_summary.get('event_count', 'unknown')}",
            f"- Worst individual high-rate stream sequence-gap estimate: {storage_summary.get('missing_frame_estimate', 'unknown')} ({storage_summary.get('missing_frame_percent', 'unknown')}%)",
            f"- Classified capture-loss frames (worst stream): {storage_summary.get('capture_loss_frame_estimate', 'unknown')}",
            f"- Processing versions: decoder {versions.get('udp_decoder', 'legacy')}; timeline {versions.get('timeline_reconstruction', 'legacy')}; event attribution {versions.get('event_attribution', 'legacy')}; continuity {versions.get('continuity_analysis', 'legacy')}; report 2",
            f"- Packets dropped by storage limit: {storage_summary.get('dropped_packets', 0)}",
        ]
    if circuit_profile:
        audit = profile_audit(circuit_profile)
        provenance = circuit_profile.get("provenance") or {}
        lines[10:10] = [
            f"- Circuit profile: {circuit_profile.get('id', 'unknown')}",
            f"- Profile verification: {circuit_profile.get('verification_status', 'unverified')}",
            f"- Corner-analysis calibration: {'ready' if audit['ready_for_corner_analysis'] else 'exploratory / not fully calibrated'}",
            f"- Profile source: {provenance.get('source', 'unavailable')}",
        ]
    valid = [lap for lap in laps if not lap.invalid]
    fastest = min(valid, key=lambda item: item.time_ms) if valid else None
    lines.extend(["", "## Engineer overview", ""])
    if report_type == "race":
        first, last = (min(laps, key=lambda item: item.number), max(laps, key=lambda item: item.number)) if laps else (None, None)
        start_position = _context(first, "start", "lap").get("position") if first else None
        finish_position = _context(last, "finish", "lap").get("position") if last else None
        lines.extend([
            f"- Completed lap files: {len(laps)} ({len(valid)} valid, {len(laps)-len(valid)} game-invalid).",
            f"- Fastest valid lap: {('lap ' + str(fastest.number) + ' — ' + _time(fastest.time_ms)) if fastest else 'unavailable'}.",
            f"- Position: {start_position if start_position is not None else 'unavailable'} → {finish_position if finish_position is not None else 'unavailable'}.",
            "- Race effects such as fuel, tyre age, traffic, weather, damage, pit state and safety car are reported as context; lap-time changes are not automatically attributed to setup or driver.",
        ])
    elif report_type == "time_trial":
        times = [lap.time_ms for lap in valid]
        lines.extend([
            f"- Included lap files: {len(laps)} ({len(valid)} valid, {len(laps)-len(valid)} game-invalid).",
            f"- Best valid lap: {('lap ' + str(fastest.number) + ' — ' + _time(fastest.time_ms)) if fastest else 'unavailable'}.",
            f"- Valid-lap consistency: mean {_time(round(mean(times))) if times else 'unavailable'}; population standard deviation {pstdev(times)/1000:.3f} s." if times else "- Valid-lap consistency: unavailable.",
            "- Invalid laps remain useful telemetry evidence but are never treated as PB candidates.",
        ])
    else:
        lines.extend([
            f"- Included laps: {len(laps)}; comparison baseline: {('fastest included valid lap ' + str(fastest.number)) if fastest else 'unavailable'}.",
            "- This report emphasizes trace evidence, setup snapshots, turn windows and operating context for the selected laps.",
        ])
    ordered_laps = sorted(laps, key=lambda item: item.number)
    setup_signatures = [
        tuple((row["key"], row["value"]) for row in setup_rows(lap.setup) if row["value"] is not None)
        for lap in ordered_laps
    ]
    if len(setup_signatures) >= 3 and setup_signatures[0] != setup_signatures[1] and len(set(setup_signatures[1:])) == 1:
        setup_audit = (storage_summary or {}).get("setup_snapshot_audit") or {}
        if setup_audit.get("status") == "confirmed_active":
            lines.append(
                f"- **Setup reconstruction:** lap {ordered_laps[0].number}'s stored snapshot is superseded. "
                f"The selected player setup began at frame {setup_audit.get('selected_frame_identifier')} and repeated unchanged "
                f"{setup_audit.get('selected_repeat_count')} times; reports retain the old snapshot as evidence and use the reconstructed setup as active context."
            )
        else:
            lines.append(
                f"- **Setup snapshot caution:** lap {ordered_laps[0].number} differs from every later lap; active setup remains uncertain."
            )
    setups = [lap.setup for lap in ordered_laps if lap.setup]
    if setups and all(setup.get("front_wing") == 0 and setup.get("rear_wing") == 0 for setup in setups):
        lines.append("- **Setup verification note:** every included setup packet decoded front/rear wing as 0/0. Values are retained exactly as transmitted; verify them in game if that is unexpected.")
    lines.extend(["", "## Lap summary", "", "| Lap | Status | Lap time | Sector 1 | Sector 2 | Samples | Coverage | Position | Compound / age | Wear finish | Fuel used | Note |", "|---:|---|---:|---:|---:|---:|---:|---|---|---|---:|---|"])
    for lap in sorted(laps, key=lambda item: item.number):
        quality = lap_quality(lap)
        coverage = f"{quality['coverage_pct']:.1f}%" if quality["coverage_pct"] is not None else "estimated/unavailable"
        note = notes.get(lap.id, "").replace("|", "\\|") or "—"
        start_lap, finish_lap = _context(lap, "start", "lap"), _context(lap, "finish", "lap")
        status, damage = _context(lap, "start", "car_status"), _context(lap, "finish", "damage")
        fuel_start, fuel_finish = status.get("fuel_kg"), _context(lap, "finish", "car_status").get("fuel_kg")
        fuel_used = float(fuel_start)-float(fuel_finish) if isinstance(fuel_start, (int,float)) and isinstance(fuel_finish, (int,float)) else None
        position = f"{start_lap.get('position', '—')}→{finish_lap.get('position', '—')}"
        lines.append(f"| {lap.number} | {'INVALID' if lap.invalid else 'valid'} | {_time(lap.time_ms)} | {_time(lap.sector1_ms)} | {_time(lap.sector2_ms)} | {len(lap.samples)} | {coverage} | {position} | {_compound(status)} / {status.get('tyre_age_laps', '—')} laps | {_wheel_text(damage.get('tyre_wear_pct'), 1, '%')} | {_number(fuel_used, 2, ' kg')} | {note} |")
    lines.append("")
    if report_type == "race" and laps:
        lines.extend(_stint_lines(laps))
    lines.extend(["## Detailed lap engineering sheets", ""])
    setup_audit = (storage_summary or {}).get("setup_snapshot_audit") or None
    for lap in sorted(laps, key=lambda item: item.number):
        lines.extend(_lap_engineering_detail(lap, notes.get(lap.id, ""), timeline or [], setup_audit))
    lines.extend(["## Circuit calibration", ""])
    if circuit_profile:
        audit = profile_audit(circuit_profile)
        dimensions = audit.get("verification_dimensions", {})
        lines.append(
            f"Profile `{circuit_profile.get('id')}` is labelled **{circuit_profile.get('verification_status', 'unverified')}**. "
            f"Corner-analysis calibration is **{'ready' if audit['ready_for_corner_analysis'] else 'exploratory'}**. "
            "Estimated apex positions are not physical survey measurements."
        )
        lines.append(
            "- Verification dimensions: "
            f"track identity {dimensions.get('track_identity', 'unknown')}; "
            f"track length {dimensions.get('track_length', 'unknown')}; "
            f"turn labels {dimensions.get('turn_labels', 'unknown')}; "
            f"corner boundaries {dimensions.get('corner_boundaries', 'unknown')}; "
            f"apex calibration {dimensions.get('apex_calibration', 'unknown')}."
        )
        for warning in audit["warnings"]:
            lines.append(f"- **Calibration warning:** {warning}")
        for turn in circuit_profile.get("turns", []):
            lines.append(
                f"- Turn {turn.get('number', '?')} — {turn.get('name') or 'unnamed'}: "
                f"{turn.get('entry_m')} / {turn.get('estimated_apex_m')} / {turn.get('exit_m')} m "
                f"(entry / estimated apex / exit), {turn.get('direction', 'unknown')}, "
                f"{turn.get('verification_status', 'unverified')}; source: {turn.get('provenance', 'unavailable')}."
            )
    else:
        lines.append("No turn profile was selected; only full-lap and sector evidence is available.")
    lines.append("")
    if storage_summary and storage_summary.get("high_rate_streams"):
        lines.extend(["## High-rate continuity", "", f"The estimate uses the {storage_summary.get('continuity_basis', 'worst individual stream')}; normal lower-rate packet schedules are excluded. A sequence gap is not called UDP loss unless recorder wall time supports that conclusion.", "", "| Stream | Received | Unique | Sequence gaps | Capture loss | Game/sequence discontinuity | Duplicates | Out of order | Largest gap | Effective receive rate |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for stream in storage_summary["high_rate_streams"]:
            lines.append(f"| {stream['name']} (ID {stream['packet_id']}) | {stream['packet_count']} | {stream.get('unique_frames', '—')} | {stream['missing_frame_estimate']} | {stream.get('capture_loss_frames', '—')} | {stream.get('sequence_discontinuity_frames', '—')} | {stream.get('duplicate_frames', '—')} | {stream.get('out_of_order_frames', '—')} | {stream['max_gap_frames']} frames | {stream.get('effective_received_hz', '—')} Hz |")
        largest = max(
            storage_summary["high_rate_streams"],
            key=lambda item: (item.get("max_gap_frames", 0), item.get("packet_id") == 2),
            default={},
        )
        lines.extend(["", "Largest classified gaps for the worst stream:", ""])
        for gap in largest.get("largest_gaps", [])[:5]:
            lines.append(f"- {gap['from_session_time']:.3f}–{gap['to_session_time']:.3f} s, {gap['missing_frames']} frames, **{gap['classification']}**: {gap['reason']}; track distance {gap.get('from_track_distance_m')}→{gap.get('to_track_distance_m')} m.")
        lines.extend(["", "A sequence gap may matter near a driver-input transition even when full-lap distance coverage is complete.", ""])
    lines.extend(["## Event reconstruction", ""])
    if timeline:
        useful_raw = [
            event for event in timeline
            if event.get("player_relevant") is not False
            and event.get("type") not in {"controller_input", "butn", "speed_trap", "damage_change"}
        ]
        useful = [event for event in useful_raw if event.get("event_validity", "accepted") == "accepted"]
        hidden_other_cars = sum(event.get("player_relevant") is False for event in timeline)
        counts = Counter(event.get("type", "event") for event in useful)
        lines.append("- Accepted-timeline player-relevant and global counts: " + ("; ".join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(counts.items())) if counts else "none") + ".")
        lines.append(f"- Superseded raw events retained: {sum(event.get('event_validity') == 'superseded' for event in timeline)}; uncertain events retained: {sum(event.get('event_validity') == 'unknown' for event in timeline)}.")
        player_index = next((event.get("player_car_index") for event in timeline if event.get("player_car_index") is not None), None)
        if player_index is not None:
            player_events = [event for event in timeline if event.get("player_relevant") is True]
            accepted_player = [event for event in player_events if event.get("event_validity", "accepted") == "accepted"]
            confirmed_made = sum(event.get("type") == "overtake" and event.get("verification_status") == "confirmed" and event.get("details", {}).get("overtaking_vehicle_index") == player_index for event in accepted_player)
            confirmed_lost = sum(event.get("type") == "overtake" and event.get("verification_status") == "confirmed" and event.get("details", {}).get("overtaken_vehicle_index") == player_index for event in accepted_player)
            raw_made = sum(event.get("type") == "overtake" and event.get("details", {}).get("overtaking_vehicle_index") == player_index for event in player_events)
            raw_lost = sum(event.get("type") == "overtake" and event.get("details", {}).get("overtaken_vehicle_index") == player_index for event in player_events)
            collisions = [event for event in accepted_player if event.get("type") == "collision"]
            incidents = {event.get("collision_incident_id") for event in collisions}
            penalty_events = [event for event in accepted_player if event.get("type") == "penalty"]
            warnings = sum(event.get("penalty_classification") == "warning" for event in penalty_events)
            actual_penalty_notifications = sum(event.get("penalty_classification") == "actual_penalty" for event in penalty_events)
            drive_throughs = sum(event.get("details", {}).get("penalty_type") == 0 for event in penalty_events)
            stop_go = sum(event.get("details", {}).get("penalty_type") == 1 for event in penalty_events)
            last_lap_context = _context(max(laps, key=lambda item: item.number), "finish", "lap") if laps else {}
            time_penalty_seconds = last_lap_context.get("penalties_s")
            confirmed_applied_penalties = sum(
                event.get("details", {}).get("penalty_type") in {0, 1, 2, 6, 7, 9}
                or (
                    event.get("details", {}).get("penalty_type") == 4
                    and isinstance(time_penalty_seconds, (int, float)) and time_penalty_seconds > 0
                )
                for event in penalty_events
            )
            superseded_penalties = sum(event.get("type") == "penalty" and event.get("player_relevant") is True and event.get("event_validity") == "superseded" for event in timeline)
            lines.extend([
                "", "### Reconstructed player statistics", "",
                f"- Player car index: {player_index}.",
                f"- Raw player overtake notifications: {raw_made} made / {raw_lost} received.",
                f"- Confirmed accepted-timeline passing manoeuvres: {confirmed_made} made / {confirmed_lost} received.",
                f"- Accepted raw collision messages: {len(collisions)}; conservatively grouped unique incidents: {len(incidents)}.",
                f"- Infringement/penalty-related UDP events: {len(penalty_events)}; warnings: {warnings}; actual-penalty-type notifications: {actual_penalty_notifications}; confirmed applied penalties: {confirmed_applied_penalties}.",
                f"- Final accepted Lap Data time-penalty counter: {time_penalty_seconds if time_penalty_seconds is not None else 'unavailable'} s; drive-through events: {drive_throughs}; stop-go events: {stop_go}.",
                f"- Removed or flashback-superseded penalty-related events: {superseded_penalties}.",
            ])
            overtakes = [event for event in player_events if event.get("type") == "overtake"]
            if overtakes:
                lines.extend(["", "### Player overtake reconciliation", "", "| Time | Lap | Direction/opponent | Distance | Player pos | Opponent pos | Timeline | Verification | Source frame |", "|---:|---:|---|---:|---|---|---|---|---:|"])
                for event in overtakes:
                    details = event["details"]
                    made = details.get("overtaking_vehicle_index") == player_index
                    opponent = details.get("overtaken_vehicle_index") if made else details.get("overtaking_vehicle_index")
                    lines.append(
                        f"| {event['session_time']:.3f} s | {event.get('lap_number') or '—'} | {'passed' if made else 'passed by'} car {opponent} | "
                        f"{_number(event.get('track_distance_m'), 1, ' m')} | {event.get('player_position_before', '—')}→{event.get('player_position_after', '—')} | "
                        f"{event.get('opponent_position_before', '—')}→{event.get('opponent_position_after', '—')} | {event.get('event_validity', 'unknown')} | "
                        f"{event.get('verification_status', 'uncertain')} | {event.get('frame_identifier', '—')} |"
                    )
            if penalty_events:
                lines.extend(["", "### Player penalty/warning audit", "", "| Time | Lap | UDP classification | Infringement | Game counters | Timeline |", "|---:|---:|---|---|---|---|"])
                for event in penalty_events:
                    lap = next((item for item in laps if item.number == event.get("lap_number")), None)
                    start_lap = _context(lap, "start", "lap") if lap else {}
                    finish_lap = _context(lap, "finish", "lap") if lap else {}
                    counters = f"penalty {start_lap.get('penalties_s', '—')}→{finish_lap.get('penalties_s', '—')} s; warnings {start_lap.get('warnings', '—')}→{finish_lap.get('warnings', '—')}"
                    lines.append(f"| {event['session_time']:.3f} s | {event.get('lap_number') or '—'} | {event.get('penalty_label', 'unknown')} | {event.get('infringement_label', 'unknown')} | {counters} | {event.get('event_validity', 'unknown')} |")
        lines.append(f"- Raw session timeline retained: {len(timeline)} events; other-car targeted events hidden from lap attribution: {hidden_other_cars}. Full evidence remains in the ZIP/SQLite export.")
        classifications = [event for event in timeline if event.get("type") == "final_classification" and isinstance(event.get("details"), dict)]
        if report_type == "race" and classifications:
            cars = classifications[-1]["details"].get("cars", [])
            lines.extend(["", "### Final classification packet", "", "| Car index | Position | Laps | Grid | Pit stops | Best lap | Penalties | Status / reason |", "|---:|---:|---:|---:|---:|---:|---:|---|"])
            for car in cars:
                lines.append(f"| {car.get('car_index', '—')} | {car.get('position', '—')} | {car.get('laps', '—')} | {car.get('grid_position', '—')} | {car.get('pit_stops', '—')} | {_time(car.get('best_lap_ms'))} | {car.get('penalties_s', '—')} s | {car.get('result_status', '—')} / {car.get('result_reason', '—')} |")
        lines.extend(["", "### Key timeline", ""])
        key_events = [
            event for event in useful_raw
            if event.get("event_validity") != "superseded"
            if event.get("type") not in {"controller_input", "butn", "speed_trap", "damage_change"}
        ]
        for event in key_events:
            lines.append(f"- {event.get('session_time', 0):.3f} s · {event.get('type', 'event')} · {event.get('source', 'unknown')} · lap {event.get('lap_number') or '—'} · {event.get('timeline_branch_id', 'unknown branch')}")
        if not key_events:
            lines.append("- No key events after filtering repetitive controller, speed-trap and damage-update messages. Full timeline remains in the ZIP export.")
    else:
        lines.append("Continuous event timeline unavailable for this legacy/lap-only capture.")
    lines.extend(["## Compatible lap deltas", ""])
    if len(valid) < 2:
        lines.append("Fewer than two valid included laps; no delta comparison available.")
    else:
        baseline = min(valid, key=lambda item: item.time_ms)
        compared = False
        for candidate in valid:
            if candidate.id == baseline.id:
                continue
            try:
                result = compare_laps(baseline, candidate, circuit_profile)
            except ComparisonError:
                continue
            compared = True
            delta = result["summary"]["final_delta_s"]
            lines.append(f"- Lap {candidate.number} vs fastest included lap {baseline.number}: {delta:+.3f} s (candidate − baseline).")
            for window in result.get("engineer_notes", {}).get("windows", []):
                window_delta = window.get("evidence", {}).get("window_delta_s")
                if isinstance(window_delta, (int, float)):
                    lines.append(f"  - {window.get('name', 'Turn')}: {window_delta:+.3f} s through the profile window; estimated apex {window.get('estimated_apex_m', 'unavailable')} m.")
        if not compared:
            lines.append("Included valid laps are not compatible under the race/Time Trial and track rules.")
    lines.extend(["", "## Interpretation limits", "", "- Observations are calculated from recorded traces; they do not establish that a setup change caused a time change.", "- Missing historical setup values are labelled unknown and are never inferred from measured tyre telemetry or defaults. Configured setup fuel is distinct from instantaneous measured fuel. Legacy coverage is an approximation when lap distance was not recorded.", "- Turn-window deltas are not additive when windows overlap; never sum them as though they partition the lap.", ""])
    return "\n".join(lines)


def build_lap_analysis_markdown(
    baseline: Any, candidate: Any, baseline_note: str, candidate_note: str,
    track_label: str, circuit_profile: dict[str, Any] | None = None,
) -> str:
    result = compare_laps(baseline, candidate, circuit_profile)
    delta = result["summary"]["final_delta_s"]
    lines = [
        f"# Apex Engineer Lap Analysis Report — {track_label}", "",
        "## Comparison identity", "",
        f"- Baseline: lap {baseline.number}, `{baseline.id}`, {_time(baseline.time_ms)}, mode {baseline.mode}, {'INVALID' if baseline.invalid else 'valid'}.",
        f"- Candidate: lap {candidate.number}, `{candidate.id}`, {_time(candidate.time_ms)}, mode {candidate.mode}, {'INVALID' if candidate.invalid else 'valid'}.",
        f"- Official final delta: {delta:+.3f} s (candidate − baseline; negative means candidate is faster).",
        f"- Alignment: {result['alignment']['baseline']} / {result['alignment']['candidate']}; {result['alignment']['points']} comparison points.",
        f"- Endpoint correction: baseline {result['endpoint_estimation']['baseline_adjustment_ms']} ms; candidate {result['endpoint_estimation']['candidate_adjustment_ms']} ms.", "",
        "## Comparison warnings and data quality", "",
        *([f"- WARNING: {warning}" for warning in result["comparison_warnings"]] or ["- No special comparison warnings."]),
        f"- Recorded distance coverage: baseline {_number(result['data_quality']['baseline_distance_coverage_pct'], 1, '%')}; candidate {_number(result['data_quality']['candidate_distance_coverage_pct'], 1, '%')}.",
        f"- Samples: baseline {result['data_quality']['baseline_sample_count']}; candidate {result['data_quality']['candidate_sample_count']}.", "",
        "## Sector and whole-lap evidence", "",
        f"- Baseline sectors: {_time(baseline.sector1_ms)} / {_time(baseline.sector2_ms)}.",
        f"- Candidate sectors: {_time(candidate.sector1_ms)} / {_time(candidate.sector2_ms)}.",
        f"- Sector deltas: {', '.join('unavailable' if value is None else f'{value:+.3f} s' for value in result['summary']['sector_deltas_s'])}.",
        f"- Maximum cumulative candidate gain/loss: {result['summary']['maximum_gain_s']:+.3f} / {result['summary']['maximum_loss_s']:+.3f} s.", "",
        "## Setup comparison", "",
        f"- Baseline: {_setup(baseline.setup)}.",
        f"- Candidate: {_setup(candidate.setup)}.", "",
        "## Circuit-profile turn analysis", "",
        f"- {result['engineer_notes']['calibration']}",
    ]
    windows = result["engineer_notes"]["windows"]
    if windows:
        for window in windows:
            evidence = window.get("evidence", {})
            lines.extend([
                f"### {window.get('name', 'Turn')}", "",
                f"- Profile window: {window.get('entry_m', 'unavailable')}–{window.get('exit_m', 'unavailable')} m; estimated apex {window.get('estimated_apex_m', 'unavailable')} m; status {window.get('verification_status', 'unverified')}; analysis {window.get('analysis_confidence', 'exploratory')}.",
                f"- Window delta: {evidence.get('window_delta_s', 0):+.3f} s.",
                *[f"- {statement}" for statement in window.get("statements", [])], "",
            ])
    else:
        lines.extend(["- No calibrated turn windows were available for this comparison.", ""])
    lines.extend(["## Baseline engineering sheet", ""])
    lines.extend(_lap_engineering_detail(baseline, baseline_note, []))
    lines.extend(["## Candidate engineering sheet", ""])
    lines.extend(_lap_engineering_detail(candidate, candidate_note, []))
    lines.extend([
        "## Interpretation limits", "",
        "- Delta and trace observations show where the recorded laps differed; they do not prove that setup, driving, tyres or conditions caused the difference.",
        "- Configured tyre pressures come only from setup packets/snapshots. Measured running pressures and temperatures are labelled separately.", "",
    ])
    return "\n".join(lines)


def build_zip(
    report: str, session: Any, laps: list[Any], raw_laps: dict[str, bytes],
    timeline: list[dict[str, Any]] | None = None, continuous_data: bytes | None = None,
    scoped_packets: bytes | None = None,
    circuit_profile: dict[str, Any] | None = None,
) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("report.md", report.encode("utf-8"))
        context = asdict(session)
        context["lap_ids"] = [lap.id for lap in laps]
        context["exported_lap_ids"] = [lap.id for lap in laps]
        archive.writestr("session.json", json.dumps(context, indent=2).encode("utf-8"))
        if circuit_profile is not None:
            archive.writestr("circuit-profile.json", json.dumps(circuit_profile, indent=2).encode("utf-8"))
        if timeline is not None:
            archive.writestr("race/timeline.json", json.dumps(timeline, indent=2).encode("utf-8"))
        if continuous_data is not None:
            archive.writestr("race/race-data.sqlite3", continuous_data)
        if scoped_packets is not None:
            archive.writestr("race/selected-packets.jsonl", scoped_packets)
        for lap in laps:
            raw = raw_laps.get(lap.id)
            if raw is None:
                raise ExportError(f"Original JSON is unavailable for lap {lap.id}")
            archive.writestr(f"laps/{safe_filename(lap.id)}.json", raw)
    return stream.getvalue()
