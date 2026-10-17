"""Circuit-profile administration and session selection policy."""

from __future__ import annotations

from typing import Any, Mapping

from ..circuit_profiles import CircuitProfileError, CircuitProfileStore
from ..domain.errors import SessionStateError
from ..domain.models import RecordingSession
from .archive import SessionArchiveRepository
from .comparisons import ComparisonPolicyError, ensure_profile_compatible


class CircuitProfileService:
    def __init__(self, profiles: CircuitProfileStore, archive: SessionArchiveRepository) -> None:
        self.profiles = profiles
        self.archive = archive

    def list_for_session(self, session: RecordingSession | None) -> list[dict[str, Any]]:
        if session and session.track_id is not None:
            return self.profiles.list(session.packet_format, session.track_id)
        return self.profiles.list(2025)

    def save(self, payload: Any) -> dict[str, Any]:
        return self.profiles.save(payload)

    def delete(
        self,
        profile_id: str,
        *,
        confirmed: bool,
        sessions: Mapping[str, RecordingSession],
    ) -> dict[str, Any]:
        if not confirmed:
            raise CircuitProfileError("Circuit profile deletion must be explicitly confirmed")
        result = self.profiles.delete(profile_id)
        affected = []
        for session in sessions.values():
            if session.circuit_profile_id == profile_id:
                session.circuit_profile_id = None
                self.archive.persist_session(session)
                affected.append(session.id)
        result["cleared_session_ids"] = affected
        return result

    def select(self, session: RecordingSession | None, profile_id: str) -> None:
        if session is None:
            raise SessionStateError("Unknown session ID")
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise SessionStateError("Unknown circuit profile")
        try:
            ensure_profile_compatible(session, profile)
        except ComparisonPolicyError as error:
            raise SessionStateError(str(error)) from error
        session.circuit_profile_id = profile_id
        self.archive.persist_session(session)
