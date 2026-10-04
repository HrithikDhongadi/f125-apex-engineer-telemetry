"""Atomic, full-fidelity personal best registry."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

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
                if isinstance(entry, dict) and isinstance(entry.get("key"), str) and isinstance(entry.get("lap"), dict):
                    self.entries[entry["key"]] = entry
            except (OSError, ValueError, TypeError):
                continue

    def _write(self, entry: dict[str, Any]) -> None:
        destination = self.directory / f"{entry['key']}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
        temporary.replace(destination)

    def consider(
        self,
        lap: Any,
        session_name: str,
        note: str = "",
        provenance: str = "Automatic completed-lap evaluation",
    ) -> dict[str, Any] | None:
        quality = lap_quality(lap)
        if lap.track_id is None or lap.track_id < 0 or lap.mode not in {"race", "time_trial"} or not quality["adequate_for_pb"]:
            return None
        key = pb_key(lap.track_id, lap.mode)
        current = self.entries.get(key)
        if current is not None and int(current["time_ms"]) <= int(lap.time_ms):
            return None
        entry = {
            "key": key, "track_id": lap.track_id, "track_length_m": lap.track_length_m,
            "mode": lap.mode, "time_ms": lap.time_ms, "sector1_ms": lap.sector1_ms,
            "sector2_ms": lap.sector2_ms, "setup": lap.setup, "became_pb_at": _now(),
            "source_session_id": lap.recording_session_id, "source_session_name": session_name,
            "source_lap_id": lap.id, "source_lap_number": lap.number, "note": note,
            "provenance": provenance, "quality": quality, "lap": asdict(lap),
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

    def summaries(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in entry.items() if key != "lap"} | {"comparison_id": f"pb:{entry['key']}"} for entry in self.entries.values()]

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)
