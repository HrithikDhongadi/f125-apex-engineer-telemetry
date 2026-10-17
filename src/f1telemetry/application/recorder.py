"""Ordered application of dependency-neutral recorder decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..domain.recording import (
    LapDataObservation,
    RecorderDecision,
    RecorderState,
    RestartEventAction,
    RewindAction,
)


@dataclass(frozen=True)
class AppliedRecorderDecision:
    code: str
    reasons: list[str]


class RecorderApplicationService:
    """Apply a boundary plan while leaving storage implementations outside.

    The caller remains the single lock/thread owner. Callbacks preserve the
    established effect order without making this service depend on SessionStore,
    files, SQLite, diagnostics, or HTTP.
    """

    @staticmethod
    def _restart_event(
        plan: RecorderDecision,
        observation: LapDataObservation,
        prior_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from_time = plan.old_lap_time_ms
        from_distance = plan.old_lap_distance_m
        if prior_event and prior_event.get("type") == "flashback":
            from_time = prior_event.get("from_lap_time_ms")
            from_distance = prior_event.get("from_lap_distance_m")
        return {
            "type": "restart_lap",
            "session_time": observation.session_time,
            "frame_identifier": observation.frame_identifier,
            "from_lap_time_ms": from_time,
            "to_lap_time_ms": plan.new_lap_time_ms,
            "from_lap_distance_m": from_distance,
            "to_lap_distance_m": plan.new_lap_distance_m,
            "game_invalid": bool(observation.packet.get("invalid", False)),
        }

    def apply(
        self,
        plan: RecorderDecision,
        state: RecorderState,
        observation: LapDataObservation,
        *,
        current_setup: dict[str, Any] | None,
        race_context: Callable[[], dict[str, Any]],
        finish_lap: Callable[[int], tuple[bool, str]],
        mark_session_recording: Callable[[], None],
    ) -> AppliedRecorderDecision:
        code = plan.code.value
        reasons = plan.reason_values

        def begin() -> None:
            state.begin(
                plan.new_lap_number, plan.capture_new_attempt,
                current_setup, race_context(),
            )

        if plan.initialize_attempt:
            begin()

        if plan.finish_time_ms is not None:
            saved, finish_reason = finish_lap(plan.finish_time_ms)
            code = "finish_saved" if saved else "finish_rejected"
            reasons.append(finish_reason)

        if plan.mark_session_recording:
            mark_session_recording()

        if plan.begin_new_attempt:
            begin()
            restart_event = self._restart_event(plan, observation)
            if plan.restart_event_action == RestartEventAction.ADD_TO_ATTEMPT:
                state.events.append(restart_event)
                state.pending_restart_event = None
            elif plan.restart_event_action == RestartEventAction.KEEP_PENDING:
                state.pending_restart_event = restart_event
            elif plan.restart_event_action == RestartEventAction.ATTACH_PENDING:
                if state.pending_restart_event is not None:
                    state.events.append(state.pending_restart_event)
                state.pending_restart_event = None
            if plan.capture_new_attempt:
                code = f"{code}_and_start_capture" if code.startswith("finish_") else "start_capture"
        elif plan.rewind_action == RewindAction.RESTART_APPROACH:
            prior_event = state.events[-1] if state.events else None
            state.samples = []
            state.events = []
            state.invalid = False
            state.setup = dict(current_setup) if current_setup else None
            state.capture_current_lap = False
            state.pending_restart_event = self._restart_event(plan, observation, prior_event)
        elif plan.rewind_action == RewindAction.FLASHBACK:
            retained = [
                sample for sample in state.samples
                if (
                    not isinstance(sample.get("current_lap_ms"), (int, float))
                    or float(sample["current_lap_ms"]) <= plan.new_lap_time_ms
                ) and (
                    not isinstance(sample.get("lap_distance_m"), (int, float))
                    or float(sample["lap_distance_m"]) <= plan.new_lap_distance_m
                )
            ]
            removed = len(state.samples) - len(retained)
            state.samples = retained
            state.invalid = False
            event = self._restart_event(plan, observation)
            event.update({"type": "flashback", "pruned_sample_count": removed})
            state.events.append(event)
        elif plan.rewind_action == RewindAction.DISCARD:
            state.samples = []
            state.events = []
            state.invalid = False
            state.setup = dict(current_setup) if current_setup else None
            state.capture_current_lap = False

        state.invalid = state.invalid or bool(observation.packet["invalid"])
        state.lap_packet.update(observation.packet)
        return AppliedRecorderDecision(code=code, reasons=reasons)
