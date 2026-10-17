"""Bounded recorder-decision diagnostics, separate from lap evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable


MAX_DIAGNOSTIC_SEGMENTS = 8
MAX_DIAGNOSTIC_EVENTS = 50_000


class DiagnosticCapture:
    def __init__(
        self,
        capture_dir: Path,
        maximum_mb: Callable[[], int],
        maximum_events: Callable[[], int] = lambda: MAX_DIAGNOSTIC_EVENTS,
        maximum_segments: Callable[[], int] = lambda: MAX_DIAGNOSTIC_SEGMENTS,
    ) -> None:
        self.capture_dir = capture_dir
        self.maximum_mb = maximum_mb
        self.maximum_events = maximum_events
        self.maximum_segments = maximum_segments
        self.enabled = False
        self.path: Path | None = None
        self.segment = -1
        self.events = 0
        self.bytes = 0
        self.truncated = False

    @staticmethod
    def timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def reset_recording(self) -> None:
        self.path = None
        self.segment = -1
        self.events = 0
        self.bytes = 0
        self.truncated = False

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.path = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active_file": str(self.path.relative_to(self.capture_dir)) if self.path else None,
            "segment": self.segment if self.path else None,
            "events": self.events,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "limits": {
                "segments": self.maximum_segments(),
                "events_per_segment": self.maximum_events(),
                "bytes_per_segment": self.maximum_mb() * 1024 * 1024,
            },
        }

    def start_segment(self, recording_session_id: str | None, reason: str) -> None:
        if not self.enabled or recording_session_id is None:
            return
        next_segment = self.segment + 1
        if next_segment >= self.maximum_segments():
            self.truncated = True
            return
        self.segment = next_segment
        self.events = 0
        self.bytes = 0
        self.truncated = False
        directory = self.capture_dir / "sessions" / recording_session_id / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(character if character.isalnum() or character == "-" else "-" for character in reason)
        self.path = directory / f"lap-data-{next_segment:02d}-{safe_reason}.jsonl"

    def write(self, recording_session_id: str | None, event: dict[str, Any]) -> None:
        if not self.enabled or recording_session_id is None or self.path is None or self.truncated:
            return
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        maximum_bytes = self.maximum_mb() * 1024 * 1024
        if self.events >= self.maximum_events() or self.bytes + len(line) > maximum_bytes:
            self.truncated = True
            marker = json.dumps({
                "event": "diagnostic_limit_reached",
                "timestamp_utc": self.timestamp(),
                "events": self.events,
                "bytes": self.bytes,
            }, separators=(",", ":")) + "\n"
            with self.path.open("ab") as stream:
                stream.write(marker.encode("utf-8"))
            return
        with self.path.open("ab") as stream:
            stream.write(line)
        self.events += 1
        self.bytes += len(line)
