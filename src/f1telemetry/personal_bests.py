"""Atomic, full-fidelity personal best registry."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from .circuits import is_known_track
from .quality import lap_quality


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pb_key(track_id: int, mode: str) -> str:
    return f"track-{track_id}__{mode}"


class PersonalBestRegistry:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("track-*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("key"), str)
                    and (isinstance(entry.get("lap"), dict) or entry.get("official_time_only") is True)
                ):
                    self.entries[entry["key"]] = entry
            except (OSError, ValueError, TypeError):
                continue

    def _write(self, entry: dict[str, Any]) -> None:
        destination = self.path_for(str(entry["key"]))
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        temporary.replace(destination)

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def consider(
        self,
        lap: Any,
        session_name: str,
        note: str = "",
        provenance: str = "Automatic completed-lap evaluation",
    ) -> dict[str, Any] | None:
        quality = lap_quality(lap)
        if not is_known_track(lap.track_id) or lap.mode not in {"race", "time_trial"} or not quality["adequate_for_pb"]:
            return None
        key = pb_key(lap.track_id, lap.mode)
        current = self.entries.get(key)
        if current is not None and int(current["time_ms"]) <= int(lap.time_ms):
            # An official Session History PB may not have a complete trace.
            # Keep its authoritative time, but retain the quickest eligible
            # captured lap as an explicitly separate comparison trace.
            comparison_time = current.get("comparison_lap_time_ms")
            if current.get("official_time_only") and (
                not isinstance(current.get("lap"), dict)
                or not isinstance(comparison_time, int)
                or int(lap.time_ms) < comparison_time
            ):
                current.update({
                    "lap": asdict(lap), "trace_available": True,
                    "comparison_lap_time_ms": lap.time_ms,
                    "comparison_source_session_id": lap.recording_session_id,
                    "comparison_source_session_name": session_name,
                    "comparison_source_lap_id": lap.id,
                    "comparison_source_lap_number": lap.number,
                })
                self._write(current)
            return None
        entry = {
            "key": key, "track_id": lap.track_id, "track_length_m": lap.track_length_m,
            "mode": lap.mode, "time_ms": lap.time_ms, "sector1_ms": lap.sector1_ms,
            "sector2_ms": lap.sector2_ms, "setup": lap.setup, "became_pb_at": _now(),
            "source_session_id": lap.recording_session_id, "source_session_name": session_name,
            "source_lap_id": lap.id, "source_lap_number": lap.number, "note": note,
            "provenance": provenance, "quality": quality, "lap": asdict(lap),
            "official_time_only": False, "trace_available": True,
            "comparison_lap_time_ms": lap.time_ms,
            "comparison_source_session_id": lap.recording_session_id,
            "comparison_source_session_name": session_name,
            "comparison_source_lap_id": lap.id,
            "comparison_source_lap_number": lap.number,
        }
        self._write(entry)
        self.entries[key] = entry
        return entry

    def consider_official_history(
        self, *, track_id: int, track_length_m: int | None, mode: str,
        time_ms: int, sector1_ms: int | None, sector2_ms: int | None,
        sector3_ms: int | None, lap_number: int, valid_flags: int,
        session_id: str, session_name: str, game_session_uid: int | None,
    ) -> dict[str, Any] | None:
        """Store an official valid lap even when its full telemetry was missed.

        Session History is authoritative for timing and validity, but it does
        not contain a telemetry trace or setup. A prior full-fidelity PB is
        retained solely as a comparison trace and is labelled separately.
        """
        if (
            not is_known_track(track_id) or mode != "time_trial"
            or time_ms < 30_000 or valid_flags != 0x0F
        ):
            return None
        key = pb_key(track_id, mode)
        current = self.entries.get(key)
        if current is not None and int(current["time_ms"]) <= int(time_ms):
            return None

        trace = current.get("lap") if isinstance(current, dict) and isinstance(current.get("lap"), dict) else None
        trace_time = int(trace["time_ms"]) if trace and isinstance(trace.get("time_ms"), int) else None
        entry = {
            "key": key, "track_id": track_id, "track_length_m": track_length_m,
            "mode": mode, "time_ms": int(time_ms), "sector1_ms": sector1_ms,
            "sector2_ms": sector2_ms, "sector3_ms": sector3_ms, "setup": None,
            "became_pb_at": _now(), "source_session_id": session_id,
            "source_session_name": session_name, "source_lap_id": None,
            "source_lap_number": int(lap_number), "note": "",
            "provenance": "Official F1 25 Session History; complete telemetry trace unavailable",
            "quality": {
                "adequate_for_pb": True, "official_timing_available": True,
                "telemetry_complete": False, "validation_source": "session_history_valid_flags",
            },
            "official_time_only": True, "trace_available": trace is not None,
            "official_history": {
                "game_session_uid": game_session_uid, "valid_flags": int(valid_flags),
                "lap_number": int(lap_number),
            },
            "lap": trace,
            "comparison_lap_time_ms": trace_time,
            "comparison_source_session_id": current.get("comparison_source_session_id", current.get("source_session_id")) if trace else None,
            "comparison_source_session_name": current.get("comparison_source_session_name", current.get("source_session_name")) if trace else None,
            "comparison_source_lap_id": current.get("comparison_source_lap_id", current.get("source_lap_id")) if trace else None,
            "comparison_source_lap_number": current.get("comparison_source_lap_number", current.get("source_lap_number")) if trace else None,
        }
        self._write(entry)
        self.entries[key] = entry
        return entry

    def rebuild(
        self,
        laps: list[Any],
        session_name: Callable[[str], str],
        note_for: Callable[[str], str],
    ) -> dict[str, int]:
        considered = updated = 0
        for lap in sorted(laps, key=lambda item: (item.time_ms, item.saved_at)):
            considered += 1
            if self.consider(
                lap, session_name(lap.recording_session_id), note_for(lap.id),
                "Historical registry rebuild",
            ) is not None:
                updated += 1
        return {"considered": considered, "updated": updated, "personal_best_count": len(self.entries)}

    def update_source_note(self, lap_id: str, note: str) -> None:
        for entry in self.entries.values():
            if entry.get("source_lap_id") == lap_id:
                entry["note"] = note
                self._write(entry)

    def update_source_setup(self, lap_id: str, setup: dict[str, Any]) -> bool:
        updated = False
        for entry in self.entries.values():
            if entry.get("source_lap_id") == lap_id or entry.get("comparison_source_lap_id") == lap_id:
                if entry.get("source_lap_id") == lap_id:
                    entry["setup"] = setup
                if isinstance(entry.get("lap"), dict):
                    entry["lap"]["setup"] = setup
                self._write(entry)
                updated = True
        return updated

    def summaries(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in entry.items() if key != "lap"} | {"comparison_id": f"pb:{entry['key']}"} for entry in self.entries.values()]

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)
