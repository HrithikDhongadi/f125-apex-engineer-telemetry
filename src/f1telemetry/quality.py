"""Shared telemetry coverage checks for reports and personal bests."""

from __future__ import annotations

from typing import Any


def lap_quality(lap: Any) -> dict[str, Any]:
    distances = [
        float(sample["lap_distance_m"])
        for sample in lap.samples
        if isinstance(sample.get("lap_distance_m"), (int, float)) and sample["lap_distance_m"] >= 0
    ]
    track_length = float(lap.track_length_m or 0)
    real_distance = len(distances) >= 2 and track_length > 100
    span = max(distances) - min(distances) if distances else 0.0
    coverage_pct = span / track_length * 100 if real_distance else None
    starts_near_line = bool(real_distance and min(distances) <= track_length * 0.05)
    ends_near_line = bool(real_distance and max(distances) >= track_length * 0.90)
    adequate = bool(
        not lap.invalid
        and lap.time_ms >= 30_000
        and len(lap.samples) >= 30
        and real_distance
        and coverage_pct is not None
        and coverage_pct >= 85
        and starts_near_line
        and ends_near_line
    )
    return {
        "sample_count": len(lap.samples),
        "alignment": "distance" if real_distance else "legacy-time",
        "distance_span_m": round(span, 1) if distances else None,
        "coverage_pct": round(coverage_pct, 1) if coverage_pct is not None else None,
        "starts_near_line": starts_near_line,
        "ends_near_line": ends_near_line,
        "adequate_for_pb": adequate,
    }
