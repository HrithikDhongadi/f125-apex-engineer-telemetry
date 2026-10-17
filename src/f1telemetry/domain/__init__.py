"""Dependency-neutral domain objects shared across Apex Engineer."""

from .errors import SessionStateError
from .models import LEGACY_SESSION_ID, Lap, RecordingSession, enrich_lap
from .recording import (
    DecisionCode,
    LapDataObservation,
    RecorderDecision,
    RecorderLifecycle,
    RecorderState,
    RecordingContext,
    ReasonCode,
    decide_lap_boundary,
)

__all__ = (
    "LEGACY_SESSION_ID",
    "Lap",
    "RecordingSession",
    "DecisionCode",
    "LapDataObservation",
    "RecorderDecision",
    "RecorderLifecycle",
    "RecorderState",
    "RecordingContext",
    "ReasonCode",
    "SessionStateError",
    "decide_lap_boundary",
    "enrich_lap",
)
