"""Dependency-neutral lap recorder state and boundary decisions.

The engine in this module interprets normalized Lap Data observations only. It
does not write files, emit diagnostics, touch SQLite, or acquire locks. Those
ordered effects remain the responsibility of the application coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RecorderLifecycle(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    CAPTURING = "capturing"


class DecisionCode(str, Enum):
    OBSERVE = "observe"
    INITIAL_OBSERVATION = "initial_observation"
    START_CAPTURE = "start_capture"
    REJECTED_CROSSING = "rejected_crossing"
    RESTART_APPROACH_ARMED = "restart_approach_armed"
    FLASHBACK_CONTINUE_CAPTURE = "flashback_continue_capture"
    DISCARD_PARTIAL_LAP = "discard_partial_lap"
    CONTINUE_CAPTURE = "continue_capture"
    CONTINUE_NOT_CAPTURING = "continue_not_capturing"


class ReasonCode(str, Enum):
    VERIFIED_RACE_GRID_START = "verified_race_grid_start"
    PRESERVED_RACE_GRID_CROSSING = "continue_race_grid_capture_across_timing_line"
    VERIFIED_ONE_SHOT_QUALIFYING_START = "verified_one_shot_qualifying_start"
    NO_PREVIOUS_PACKET = "no_previous_lap_packet_so_full_lap_start_is_unproven"
    CROSSING_WITHOUT_OFFICIAL_TIME = "crossing_has_no_official_completed_lap_time"
    VERIFIED_RESTART_START = "verified_start_restart_at_lap_start"
    VERIFIED_NUMBER_ADVANCE = "verified_start_lap_number_advanced"
    VERIFIED_SAME_NUMBER_WRAP = "verified_start_same_number_wrap"
    VERIFIED_NEGATIVE_DISTANCE = "verified_start_negative_distance_crossing"
    REJECTED_NUMBER_ADVANCE = "rejected_crossing_lap_number_advanced_but_time_or_distance_not_near_line"
    REJECTED_NEAR_LINE = "rejected_crossing_near_line_without_transition_evidence"
    ACTIVE_LAP_CHANGED = "active_lap_changed_without_verified_crossing_capture_disabled"
    LAP_NUMBER_DECREASED = "discard_lap_number_decreased_probable_restart_or_flashback"
    RESTART_RELOCATED = "restart_lap_relocated_to_untimed_approach"
    FLASHBACK_PRUNED = "flashback_pruned_superseded_samples_capture_continues"
    DISCARD_TIME_AND_DISTANCE = "discard_time_and_distance_reversed"
    DISCARD_TIME = "discard_time_reversed"
    DISCARD_DISTANCE = "discard_distance_reversed"
    CONTINUE_CURRENT_LAP = "continue_current_lap"


class RewindAction(str, Enum):
    NONE = "none"
    RESTART_APPROACH = "restart_approach"
    FLASHBACK = "flashback"
    DISCARD = "discard"


class RestartEventAction(str, Enum):
    NONE = "none"
    ADD_TO_ATTEMPT = "add_to_attempt"
    KEEP_PENDING = "keep_pending"
    ATTACH_PENDING = "attach_pending"


@dataclass
class RecorderState:
    """All mutable state for the current lap attempt.

    Sample/event lists deliberately stay mutable: copying a full trace for each
    high-rate packet would add cost without improving transition determinism.
    """

    active_lap_number: int | None = None
    invalid: bool = False
    samples: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    pending_restart_event: dict[str, Any] | None = None
    lap_packet: dict[str, Any] = field(default_factory=dict)
    setup: dict[str, Any] | None = None
    race_context: dict[str, Any] | None = None
    capture_current_lap: bool = False

    @property
    def lifecycle(self) -> RecorderLifecycle:
        if self.active_lap_number is None:
            return RecorderLifecycle.IDLE
        return RecorderLifecycle.CAPTURING if self.capture_current_lap else RecorderLifecycle.ARMED

    def reset(self) -> None:
        self.active_lap_number = None
        self.invalid = False
        self.samples = []
        self.events = []
        self.pending_restart_event = None
        self.lap_packet = {}
        self.setup = None
        self.race_context = None
        self.capture_current_lap = False

    def begin(
        self, lap_number: int, capture: bool,
        setup: dict[str, Any] | None, race_context: dict[str, Any] | None,
    ) -> None:
        self.active_lap_number = lap_number
        self.invalid = False
        self.samples = []
        self.events = []
        self.lap_packet = {}
        self.setup = dict(setup) if setup else None
        self.race_context = race_context
        self.capture_current_lap = capture


@dataclass(frozen=True)
class LapDataObservation:
    packet: dict[str, Any]
    session_uid: int | None = None
    session_time: float | None = None
    frame_identifier: int | None = None
    overall_frame_identifier: int | None = None

    @classmethod
    def from_packet(
        cls, packet: dict[str, Any], session_uid: int | None,
        packet_meta: dict[str, Any] | None,
    ) -> "LapDataObservation":
        meta = packet_meta or {}
        return cls(
            packet=dict(packet), session_uid=session_uid,
            session_time=meta.get("session_time"),
            frame_identifier=meta.get("frame_identifier"),
            overall_frame_identifier=meta.get("overall_frame_identifier"),
        )


@dataclass(frozen=True)
class RecordingContext:
    recording_session_id: str | None
    mode: str | None
    game_session_type: int | None
    status: str | None
    track_length_m: int | None


@dataclass
class RecorderDecision:
    code: DecisionCode = DecisionCode.OBSERVE
    reasons: list[ReasonCode] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    initialize_attempt: bool = False
    begin_new_attempt: bool = False
    capture_new_attempt: bool = False
    mark_session_recording: bool = False
    finish_time_ms: int | None = None
    diagnostic_segment: str | None = None
    rewind_action: RewindAction = RewindAction.NONE
    restart_event_action: RestartEventAction = RestartEventAction.NONE
    old_lap_number: int | None = None
    old_lap_time_ms: int | None = None
    old_lap_distance_m: float | None = None
    new_lap_number: int = 0
    new_lap_time_ms: int = 0
    new_lap_distance_m: float = 0.0

    @property
    def reason_values(self) -> list[str]:
        return [reason.value for reason in self.reasons]


ONE_SHOT_SESSION_TYPES = {9, 14}


def decide_lap_boundary(
    state: RecorderState,
    observation: LapDataObservation,
    context: RecordingContext,
) -> RecorderDecision:
    """Return the ordered boundary plan without performing external effects.

    Thresholds and precedence intentionally match the characterized legacy
    recorder. Changes here require explicit packet-sequence regression evidence.
    """

    packet = observation.packet
    lap_number = int(packet["lap_number"])
    decision = RecorderDecision(
        old_lap_number=state.active_lap_number,
        new_lap_number=lap_number,
        new_lap_time_ms=int(packet.get("current_lap_ms", 0)),
        new_lap_distance_m=float(packet.get("lap_distance_m", 0)),
    )
    if state.active_lap_number is None:
        opening = bool(
            context.recording_session_id is not None
            and (context.mode == "race" or context.game_session_type in ONE_SHOT_SESSION_TYPES)
            and context.status == "armed"
            and lap_number == 1
            and int(packet.get("last_lap_ms", 0)) == 0
            and int(packet.get("current_lap_ms", 0)) <= 1_500
            and isinstance(observation.session_time, (int, float))
            and 0 <= float(observation.session_time) <= 5
        )
        decision.initialize_attempt = True
        decision.capture_new_attempt = opening
        decision.mark_session_recording = opening
        decision.flags = {
            "session_opening_start": opening,
            "race_grid_start": opening and context.mode == "race",
            "one_shot_qualifying_start": opening and context.game_session_type in ONE_SHOT_SESSION_TYPES,
        }
        if opening:
            decision.code = DecisionCode.START_CAPTURE
            decision.reasons.append(
                ReasonCode.VERIFIED_RACE_GRID_START
                if context.mode == "race" else ReasonCode.VERIFIED_ONE_SHOT_QUALIFYING_START
            )
        else:
            decision.code = DecisionCode.INITIAL_OBSERVATION
            decision.reasons.append(ReasonCode.NO_PREVIOUS_PACKET)
        return decision

    old_number = state.active_lap_number
    old_time_value = state.lap_packet.get("current_lap_ms")
    old_distance_value = state.lap_packet.get("lap_distance_m")
    old_time = int(old_time_value) if old_time_value is not None else None
    old_distance = float(old_distance_value) if old_distance_value is not None else None
    new_time = decision.new_lap_time_ms
    new_distance = decision.new_lap_distance_m
    track_length = float(context.track_length_m or 0)
    time_reversed = old_time is not None and new_time + 1_000 < old_time
    distance_reversed = old_distance is not None and new_distance + 50 < old_distance
    lap_number_reversed = lap_number < old_number
    near_line = new_time <= 5_000 and -200 <= new_distance <= max(250, track_length * 0.05)
    forward_number = lap_number > old_number
    old_total_distance = state.lap_packet.get("total_distance_m")
    new_total_distance = packet.get("total_distance_m")
    total_distance_step = (
        float(new_total_distance) - float(old_total_distance)
        if old_total_distance is not None and new_total_distance is not None else None
    )
    total_distance_reversed = bool(total_distance_step is not None and total_distance_step < -50)
    continuous_forward_distance = bool(
        total_distance_step is not None
        and -5 <= total_distance_step <= max(250, track_length * 0.05)
    )
    timed_same_number_wrap = bool(old_time is not None and old_time > 0 and new_time + 1_000 < old_time)
    untimed_approach_wrap = bool(old_time is not None and old_time == 0 and 0 <= new_time <= 5_000)
    wrapped_same_number = bool(
        lap_number == old_number and track_length > 100 and old_distance is not None
        and old_distance >= track_length * 0.85 and new_distance <= track_length * 0.05
        and continuous_forward_distance and (timed_same_number_wrap or untimed_approach_wrap)
    )
    crossed_from_negative = bool(
        lap_number == old_number and old_distance is not None
        and -200 <= old_distance < 0 <= new_distance and new_time <= 5_000
    )
    preserve_race_grid_crossing = bool(
        crossed_from_negative
        and context.mode == "race"
        and old_number == lap_number == 1
        and state.capture_current_lap
        and int(packet.get("last_lap_ms", 0)) == 0
        and old_time is not None and old_time <= 5_000
    )
    reset_to_start = bool(
        lap_number <= old_number and (time_reversed or distance_reversed or total_distance_reversed)
        and old_distance is not None and old_distance > max(250, track_length * 0.05)
        and not continuous_forward_distance and new_time <= 5_000 and -200 <= new_distance <= 100
    )
    restart_lap_started = reset_to_start and new_distance >= 0
    restart_approach = bool(
        lap_number <= old_number and total_distance_reversed and new_time <= 5_000
    )
    crossed_start_line = bool(
        near_line
        and (forward_number or wrapped_same_number or crossed_from_negative)
        and not preserve_race_grid_crossing
    )
    new_lap_started = crossed_start_line or restart_lap_started
    completed_lap_available = crossed_start_line and int(packet.get("last_lap_ms", 0)) >= 30_000

    decision.old_lap_time_ms = old_time
    decision.old_lap_distance_m = old_distance
    decision.flags = {
        "near_line": near_line, "forward_number": forward_number,
        "wrapped_same_number": wrapped_same_number,
        "timed_same_number_wrap": timed_same_number_wrap,
        "untimed_approach_wrap": untimed_approach_wrap,
        "total_distance_step_m": total_distance_step,
        "total_distance_reversed": total_distance_reversed,
        "continuous_forward_distance": continuous_forward_distance,
        "crossed_from_negative": crossed_from_negative,
        "preserve_race_grid_crossing": preserve_race_grid_crossing,
        "reset_to_start": reset_to_start,
        "restart_lap_started": restart_lap_started,
        "restart_approach": restart_approach,
        "new_lap_started": new_lap_started,
        "completed_lap_available": completed_lap_available,
    }
    if reset_to_start or restart_approach:
        decision.diagnostic_segment = "restart-lap"
    elif lap_number_reversed or ((time_reversed or distance_reversed or total_distance_reversed) and not new_lap_started):
        decision.diagnostic_segment = "lap-number-rewind" if lap_number_reversed else "time-distance-rewind"

    if preserve_race_grid_crossing:
        decision.reasons.append(ReasonCode.PRESERVED_RACE_GRID_CROSSING)
        decision.code = DecisionCode.CONTINUE_CAPTURE

    if completed_lap_available and state.capture_current_lap:
        decision.finish_time_ms = int(packet["last_lap_ms"])
    elif crossed_start_line and state.capture_current_lap:
        decision.reasons.append(ReasonCode.CROSSING_WITHOUT_OFFICIAL_TIME)

    if new_lap_started and context.recording_session_id is not None:
        decision.mark_session_recording = context.status == "armed"
        decision.capture_new_attempt = context.status in {"armed", "recording"}
        if restart_lap_started:
            decision.reasons.append(ReasonCode.VERIFIED_RESTART_START)
        elif forward_number:
            decision.reasons.append(ReasonCode.VERIFIED_NUMBER_ADVANCE)
        elif wrapped_same_number:
            decision.reasons.append(ReasonCode.VERIFIED_SAME_NUMBER_WRAP)
        else:
            decision.reasons.append(ReasonCode.VERIFIED_NEGATIVE_DISTANCE)
        if decision.capture_new_attempt:
            decision.code = DecisionCode.START_CAPTURE
    else:
        if forward_number and not near_line:
            decision.reasons.append(ReasonCode.REJECTED_NUMBER_ADVANCE)
            decision.code = DecisionCode.REJECTED_CROSSING
        elif near_line and not (forward_number or wrapped_same_number or crossed_from_negative):
            decision.reasons.append(ReasonCode.REJECTED_NEAR_LINE)
            decision.code = DecisionCode.REJECTED_CROSSING

    decision.begin_new_attempt = lap_number != old_number or new_lap_started
    if decision.begin_new_attempt:
        if lap_number != old_number and not new_lap_started:
            decision.reasons.append(ReasonCode.ACTIVE_LAP_CHANGED)
            if lap_number_reversed:
                decision.reasons.append(ReasonCode.LAP_NUMBER_DECREASED)
                decision.code = DecisionCode.DISCARD_PARTIAL_LAP
        if restart_lap_started:
            decision.restart_event_action = RestartEventAction.ADD_TO_ATTEMPT
        elif reset_to_start:
            decision.restart_event_action = RestartEventAction.KEEP_PENDING
        elif crossed_start_line and decision.capture_new_attempt and state.pending_restart_event is not None:
            decision.restart_event_action = RestartEventAction.ATTACH_PENDING
        return decision

    if time_reversed or distance_reversed or total_distance_reversed:
        if restart_approach:
            decision.rewind_action = RewindAction.RESTART_APPROACH
            decision.reasons.append(ReasonCode.RESTART_RELOCATED)
            decision.code = DecisionCode.RESTART_APPROACH_ARMED
        elif state.capture_current_lap and lap_number == old_number:
            decision.rewind_action = RewindAction.FLASHBACK
            decision.reasons.append(ReasonCode.FLASHBACK_PRUNED)
            decision.code = DecisionCode.FLASHBACK_CONTINUE_CAPTURE
        else:
            decision.rewind_action = RewindAction.DISCARD
            if time_reversed and distance_reversed:
                decision.reasons.append(ReasonCode.DISCARD_TIME_AND_DISTANCE)
            elif time_reversed:
                decision.reasons.append(ReasonCode.DISCARD_TIME)
            else:
                decision.reasons.append(ReasonCode.DISCARD_DISTANCE)
            decision.code = DecisionCode.DISCARD_PARTIAL_LAP
    elif not decision.reasons:
        decision.reasons.append(ReasonCode.CONTINUE_CURRENT_LAP)
        decision.code = (
            DecisionCode.CONTINUE_CAPTURE
            if state.capture_current_lap else DecisionCode.CONTINUE_NOT_CAPTURING
        )
    return decision
