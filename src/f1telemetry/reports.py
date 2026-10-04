"""Read-only Markdown and full-fidelity ZIP exports."""

from __future__ import annotations

from dataclasses import asdict
from io import BytesIO
import json
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .analysis import ComparisonError, compare_laps
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


def build_markdown(session: Any, laps: list[Any], notes: dict[str, str], track_label: str) -> str:
    lines = [
        f"# Apex Engineer report — {session.name}", "",
        "## Session", "",
        f"- Session ID: `{session.id}`", f"- Circuit: {track_label} (track ID {session.track_id if session.track_id is not None else 'unknown'})",
        f"- Game mode: {session.mode}", f"- Capture started: {session.started_at or 'unavailable'}",
        f"- Capture ended: {session.ended_at or 'unavailable'}", f"- Status: {session.status}",
        f"- Included laps: {', '.join(str(lap.number) for lap in laps) if laps else 'none'}", "",
        "## Lap summary", "",
        "| Lap | Status | Lap time | Sector 1 | Sector 2 | Samples | Coverage | Setup | Note |",
        "|---:|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for lap in laps:
        quality = lap_quality(lap)
        coverage = f"{quality['coverage_pct']:.1f}%" if quality["coverage_pct"] is not None else "estimated/unavailable"
        note = notes.get(lap.id, "").replace("|", "\\|") or "—"
        lines.append(f"| {lap.number} | {'INVALID' if lap.invalid else 'valid'} | {_time(lap.time_ms)} | {_time(lap.sector1_ms)} | {_time(lap.sector2_ms)} | {len(lap.samples)} | {coverage} | {_setup(lap.setup)} | {note} |")
    lines.extend(["", "## Full setup snapshots", ""])
    for lap in laps:
        lines.extend([f"### Lap {lap.number}", ""])
        for row in setup_rows(lap.setup):
            value = "unknown" if row["value"] is None else f"{row['value']}{(' ' + row['unit']) if row['unit'] else ''}"
            source = "manually supplied" if row["source"] == "manual" else "decoded from UDP" if row["source"] else "unknown"
            lines.append(f"- {row['label']}: {value} — {source}")
        lines.append("")
    lines.extend(["## Trace observations", ""])
    for lap in laps:
        quality = lap_quality(lap)
        lines.extend([
            f"### Lap {lap.number} — {_time(lap.time_ms)}{' — INVALID' if lap.invalid else ''}", "",
            f"- Data quality: {quality['alignment']}; distance span {quality['distance_span_m'] if quality['distance_span_m'] is not None else 'unavailable'} m; PB-eligible coverage: {'yes' if quality['adequate_for_pb'] else 'no'}.",
            f"- Recorded timeline events: {len(lap.events)}.",
            *[
                f"  - {event.get('type', 'event')}: {event.get('from_lap_time_ms', 'unknown')}→{event.get('to_lap_time_ms', 'unknown')} ms, "
                f"{event.get('from_lap_distance_m', 'unknown')}→{event.get('to_lap_distance_m', 'unknown')} m"
                for event in lap.events
            ],
            *[f"- {item}" for item in _observations(lap)], "",
        ])
    lines.extend(["## Compatible lap deltas", ""])
    valid = [lap for lap in laps if not lap.invalid]
    if len(valid) < 2:
        lines.append("Fewer than two valid included laps; no delta comparison available.")
    else:
        baseline = min(valid, key=lambda item: item.time_ms)
        compared = False
        for candidate in valid:
            if candidate.id == baseline.id:
                continue
            try:
                result = compare_laps(baseline, candidate)
            except ComparisonError:
                continue
            compared = True
            delta = result["summary"]["final_delta_s"]
            lines.append(f"- Lap {candidate.number} vs fastest included lap {baseline.number}: {delta:+.3f} s (candidate − baseline).")
        if not compared:
            lines.append("Included valid laps are not compatible under the race/Time Trial and track rules.")
    lines.extend(["", "## Interpretation limits", "", "- Observations are calculated from recorded traces; they do not establish that a setup change caused a time change.", "- Missing historical setup values are labelled unknown and are never inferred from measured tyre telemetry or defaults. Legacy coverage is an approximation when lap distance was not recorded.", ""])
    return "\n".join(lines)


def build_zip(report: str, session: Any, laps: list[Any], raw_laps: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("report.md", report.encode("utf-8"))
        context = asdict(session)
        context["lap_ids"] = [lap.id for lap in laps]
        context["exported_lap_ids"] = [lap.id for lap in laps]
        archive.writestr("session.json", json.dumps(context, indent=2).encode("utf-8"))
        for lap in laps:
            raw = raw_laps.get(lap.id)
            if raw is None:
                raise ExportError(f"Original JSON is unavailable for lap {lap.id}")
            archive.writestr(f"laps/{safe_filename(lap.id)}.json", raw)
    return stream.getvalue()
