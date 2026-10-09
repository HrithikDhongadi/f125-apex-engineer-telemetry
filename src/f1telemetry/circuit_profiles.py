"""Versioned, editable circuit profiles keyed by the game's stable track ID."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .circuits import track_name


PROFILE_SCHEMA_VERSION = 1

SILVERSTONE_SEED: dict[str, Any] = {
    "id": "f1-2025-track-7-normal-v1",
    "schema_version": PROFILE_SCHEMA_VERSION,
    "packet_format": 2025,
    "track_id": 7,
    "layout": "normal",
    "circuit_name": "Silverstone",
    "measured_game_length_m": 5890,
    "sector_boundaries_m": [],
    "centreline": [],
    "provenance": {
        "source": "Migrated from Apex Engineer's normalized Silverstone windows",
        "license": "project-authored",
        "notes": "Approximate seed only; not calibrated against an F1 25 distance trace.",
    },
    "verification_status": "approximate",
    "turns": [
        {"number": "1–3", "name": "Abbey–Farm", "entry_m": 0, "estimated_apex_m": 383, "exit_m": 766, "direction": "mixed", "linked_group": "Abbey–Farm", "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "3–4", "name": "Village–Loop", "entry_m": 766, "estimated_apex_m": 1119, "exit_m": 1473, "direction": "mixed", "linked_group": "Village–Loop", "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "6–8", "name": "Luffield–Woodcote", "entry_m": 1885, "estimated_apex_m": 2297, "exit_m": 2709, "direction": "mixed", "linked_group": "Luffield–Woodcote", "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "9", "name": "Copse", "entry_m": 2709, "estimated_apex_m": 3033, "exit_m": 3357, "direction": "right", "linked_group": None, "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "10–14", "name": "Maggotts–Becketts–Chapel", "entry_m": 3357, "estimated_apex_m": 3798, "exit_m": 4241, "direction": "mixed", "linked_group": "Maggotts–Becketts–Chapel", "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "15", "name": "Hangar Straight", "entry_m": 4241, "estimated_apex_m": 4536, "exit_m": 4830, "direction": "straight", "linked_group": None, "provenance": "legacy normalized window", "verification_status": "approximate"},
        {"number": "15–18", "name": "Stowe–Vale–Club", "entry_m": 4830, "estimated_apex_m": 5360, "exit_m": 5890, "direction": "mixed", "linked_group": "Stowe–Vale–Club", "provenance": "legacy normalized window", "verification_status": "approximate"},
    ],
}


class CircuitProfileError(ValueError):
    pass


def layout_for_track(track_id: int) -> str:
    return "reverse" if track_id in {39, 40, 41} else "normal"


def profile_identity(packet_format: int, track_id: int, layout: str | None = None) -> str:
    return f"f1-{packet_format}-track-{track_id}-{layout or layout_for_track(track_id)}-v{PROFILE_SCHEMA_VERSION}"


def profile_audit(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Separate a profile-level label from per-corner calibration evidence."""
    if not profile:
        return {"ready_for_corner_analysis": False, "warnings": ["No circuit profile selected."], "overlaps": []}
    turns = [turn for turn in profile.get("turns", []) if isinstance(turn, dict)]
    unverified = [str(turn.get("number", "?")) for turn in turns if turn.get("verification_status") != "verified"]
    unnamed = [str(turn.get("number", "?")) for turn in turns if not str(turn.get("name") or "").strip() or str(turn.get("name") or "").lower() == "suggested turn"]
    overlaps = []
    ordered = sorted(turns, key=lambda turn: float(turn.get("entry_m", 0)))
    for left, right in zip(ordered, ordered[1:]):
        if float(right.get("entry_m", 0)) < float(left.get("exit_m", 0)):
            overlaps.append({
                "left": str(left.get("number", "?")), "right": str(right.get("number", "?")),
                "overlap_m": round(float(left["exit_m"]) - float(right["entry_m"]), 1),
            })
    warnings = []
    if profile.get("verification_status") == "verified" and unverified:
        warnings.append("Profile is labelled verified, but individual turns remain unverified: " + ", ".join(unverified) + ".")
    if unnamed:
        warnings.append("Unnamed or draft turn labels remain: " + ", ".join(unnamed) + ".")
    if overlaps:
        warnings.append("Turn windows overlap and their deltas are not additive: " + ", ".join(f"{item['left']}/{item['right']} ({item['overlap_m']} m)" for item in overlaps) + ".")
    ready = bool(turns) and profile.get("verification_status") == "verified" and not unverified and not unnamed and not overlaps
    return {
        "ready_for_corner_analysis": ready, "warnings": warnings, "overlaps": overlaps,
        "unverified_turns": unverified, "unnamed_turns": unnamed,
        "verification_dimensions": {
            "track_identity": "verified" if isinstance(profile.get("track_id"), int) else "unknown",
            "track_length": "measured_in_game" if isinstance(profile.get("measured_game_length_m"), (int, float)) else "unknown",
            "turn_labels": "verified" if turns and not unnamed and not unverified else "unverified",
            "corner_boundaries": "verified" if turns and not unverified and not overlaps else "unverified",
            "apex_calibration": "verified" if turns and not unverified else "estimated_or_unverified",
        },
    }


class CircuitProfileStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, profile_id: str) -> Path:
        safe = "".join(char for char in profile_id if char.isalnum() or char in "-_")
        if not safe or safe != profile_id:
            raise CircuitProfileError("Invalid profile ID")
        return self.directory / f"{safe}.json"

    def get(self, profile_id: str) -> dict[str, Any] | None:
        path = self._path(profile_id)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                return None
        if profile_id == SILVERSTONE_SEED["id"]:
            return deepcopy(SILVERSTONE_SEED)
        return None

    def default(self, packet_format: int, track_id: int) -> dict[str, Any] | None:
        return self.get(profile_identity(packet_format, track_id))

    def list(self, packet_format: int | None = None, track_id: int | None = None) -> list[dict[str, Any]]:
        profiles = [deepcopy(SILVERSTONE_SEED)]
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    profiles = [item for item in profiles if item.get("id") != payload.get("id")]
                    profiles.append(payload)
            except (OSError, ValueError):
                continue
        if packet_format is not None:
            profiles = [item for item in profiles if item.get("packet_format") == packet_format]
        if track_id is not None:
            profiles = [item for item in profiles if item.get("track_id") == track_id]
        return profiles

    def save(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CircuitProfileError("Profile must be a JSON object")
        try:
            packet_format = int(payload.get("packet_format", 2025))
            track_id = int(payload["track_id"])
            length = float(payload["measured_game_length_m"])
        except (KeyError, TypeError, ValueError) as error:
            raise CircuitProfileError("packet_format, track_id and measured_game_length_m are required") from error
        if packet_format != 2025 or length <= 100:
            raise CircuitProfileError("Only F1 25 profiles with a plausible game length are supported")
        layout = str(payload.get("layout") or layout_for_track(track_id))
        if layout not in {"normal", "reverse", "alternate"}:
            raise CircuitProfileError("layout must be normal, reverse or alternate")
        verification = str(payload.get("verification_status", "unverified"))
        if verification not in {"unverified", "approximate", "verified"}:
            raise CircuitProfileError("verification_status must be unverified, approximate or verified")
        turns = payload.get("turns", [])
        if not isinstance(turns, list):
            raise CircuitProfileError("turns must be a list")
        clean_turns = []
        for position, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise CircuitProfileError(f"Turn {position + 1} must be an object")
            try:
                entry, apex, exit_distance = (float(turn[key]) for key in ("entry_m", "estimated_apex_m", "exit_m"))
            except (KeyError, TypeError, ValueError) as error:
                raise CircuitProfileError(f"Turn {position + 1} needs numeric entry, apex and exit distances") from error
            if not 0 <= entry <= apex <= exit_distance <= length:
                raise CircuitProfileError(f"Turn {position + 1} must satisfy 0 ≤ entry ≤ apex ≤ exit ≤ track length")
            turn_verification = str(turn.get("verification_status", "unverified"))
            if turn_verification not in {"unverified", "approximate", "verified"}:
                raise CircuitProfileError(f"Turn {position + 1} has an invalid verification status")
            clean_turns.append({
                "number": str(turn.get("number", position + 1))[:20], "name": str(turn.get("name", ""))[:80],
                "entry_m": entry, "estimated_apex_m": apex, "exit_m": exit_distance,
                "direction": str(turn.get("direction", "unknown"))[:20],
                "linked_group": str(turn["linked_group"])[:80] if turn.get("linked_group") else None,
                "provenance": str(turn.get("provenance", "manual dashboard edit"))[:200],
                "verification_status": turn_verification,
            })
        profile_id = str(payload.get("id") or profile_identity(packet_format, track_id, layout))
        result = {
            "id": profile_id, "schema_version": PROFILE_SCHEMA_VERSION, "packet_format": packet_format,
            "track_id": track_id, "layout": layout,
            "circuit_name": str(payload.get("circuit_name") or track_name(track_id))[:80],
            "measured_game_length_m": length,
            "sector_boundaries_m": [float(value) for value in payload.get("sector_boundaries_m", [])][:2],
            "centreline": payload.get("centreline", []), "turns": clean_turns,
            "provenance": payload.get("provenance") or {"source": "manual dashboard edit"},
            "verification_status": verification,
        }
        path = self._path(profile_id)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        temporary.replace(path)
        return result

    def delete(self, profile_id: str) -> dict[str, Any]:
        if profile_id == SILVERSTONE_SEED["id"]:
            raise CircuitProfileError("The built-in Silverstone seed cannot be deleted")
        path = self._path(profile_id)
        if not path.exists():
            raise CircuitProfileError("Unknown circuit profile")
        trash = self.directory.parent / "trash" / "circuit_profiles"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = trash / f"{path.stem}--deleted-{stamp}-{uuid4().hex[:8]}.json"
        try:
            path.replace(destination)
        except OSError as error:
            raise CircuitProfileError("Could not move the circuit profile to local trash") from error
        return {
            "deleted_profile_id": profile_id,
            "recoverable": True,
            "moved_to": str(destination.relative_to(self.directory.parent)),
        }
