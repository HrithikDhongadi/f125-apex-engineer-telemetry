"""Shared profile selection and cross-session comparison policy."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..domain.models import Lap, RecordingSession


class ProfileReader(Protocol):
    def get(self, profile_id: str) -> dict[str, Any] | None: ...


class ComparisonPolicyError(ValueError):
    pass


def ensure_profile_compatible(session: RecordingSession, profile: dict[str, Any]) -> None:
    if profile.get("packet_format") != session.packet_format or profile.get("track_id") != session.track_id:
        raise ComparisonPolicyError("Circuit profile is incompatible with this session's format or track ID")


def resolve_comparison_profile(
    baseline: Lap,
    candidate: Lap,
    sessions: Mapping[str, RecordingSession],
    profiles: ProfileReader,
) -> dict[str, Any] | None:
    baseline_session = sessions.get(baseline.recording_session_id)
    candidate_session = sessions.get(candidate.recording_session_id)
    baseline_profile_id = baseline_session.circuit_profile_id if baseline_session else None
    candidate_profile_id = candidate_session.circuit_profile_id if candidate_session else None
    if (
        baseline.mode == candidate.mode == "race"
        and baseline.recording_session_id != candidate.recording_session_id
        and (
            baseline_session is None
            or candidate_session is None
            or baseline_profile_id != candidate_profile_id
        )
    ):
        raise ComparisonPolicyError("Cross-session race laps must select the same verified circuit profile")
    profile = profiles.get(baseline_profile_id) if baseline_profile_id else None
    if profile is None and candidate_profile_id:
        profile = profiles.get(candidate_profile_id)
    return profile
