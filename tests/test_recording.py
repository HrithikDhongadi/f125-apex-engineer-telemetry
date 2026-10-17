from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.f1telemetry.application.recorder import RecorderApplicationService
from src.f1telemetry.domain.recording import (
    DecisionCode,
    LapDataObservation,
    RecorderLifecycle,
    RecorderState,
    RecordingContext,
    ReasonCode,
    RewindAction,
    decide_lap_boundary,
)
from src.f1telemetry.protocol import PACKET_LAP_DATA
from src.f1telemetry.receiver import UdpReceiver


def packet(number, current_ms, distance_m, last_ms=0, total_distance_m=None, invalid=False):
    return {
        "lap_number": number,
        "current_lap_ms": current_ms,
        "last_lap_ms": last_ms,
        "lap_distance_m": distance_m,
        "total_distance_m": distance_m if total_distance_m is None else total_distance_m,
        "invalid": invalid,
    }


def context(status="armed", mode="time_trial", session_type=18):
    return RecordingContext("session", mode, session_type, status, 5890)


class RecorderDecisionTests(unittest.TestCase):
    def test_first_full_time_trial_lap_starts_only_at_verified_crossing(self):
        state = RecorderState()
        first = decide_lap_boundary(
            state, LapDataObservation(packet(19, 89_000, 5885)), context(),
        )
        self.assertEqual(first.code, DecisionCode.INITIAL_OBSERVATION)
        self.assertEqual(first.reasons, [ReasonCode.NO_PREVIOUS_PACKET])
        state.begin(19, False, None, {})
        state.lap_packet.update(packet(19, 89_000, 5885, total_distance_m=117_785))

        crossing = decide_lap_boundary(
            state,
            LapDataObservation(packet(20, 16, .45, 89_000, 117_790)),
            context(),
        )
        self.assertTrue(crossing.begin_new_attempt)
        self.assertTrue(crossing.capture_new_attempt)
        self.assertIsNone(crossing.finish_time_ms)
        self.assertIn(ReasonCode.VERIFIED_NUMBER_ADVANCE, crossing.reasons)

    def test_same_number_restart_at_start_is_a_new_captured_attempt(self):
        state = RecorderState(active_lap_number=38, capture_current_lap=True)
        state.lap_packet.update(packet(38, 45_000, 3000, total_distance_m=220_000))
        decision = decide_lap_boundary(
            state,
            LapDataObservation(packet(38, 100, 5, total_distance_m=216_995)),
            context(status="recording"),
        )
        self.assertTrue(decision.flags["restart_lap_started"])
        self.assertTrue(decision.begin_new_attempt)
        self.assertTrue(decision.capture_new_attempt)
        self.assertIn(ReasonCode.VERIFIED_RESTART_START, decision.reasons)

    def test_flashback_decision_prunes_only_superseded_samples_and_uses_game_validity(self):
        state = RecorderState(active_lap_number=3, capture_current_lap=True, invalid=True)
        state.lap_packet.update(packet(3, 46_000, 2800, total_distance_m=20_000, invalid=True))
        state.samples = [
            {"current_lap_ms": 20_000, "lap_distance_m": 1200},
            {"current_lap_ms": 40_000, "lap_distance_m": 2500},
        ]
        observation = LapDataObservation(
            packet(3, 33_000, 2100, total_distance_m=19_300, invalid=False),
            session_time=50.0, frame_identifier=123,
        )
        decision = decide_lap_boundary(state, observation, context(status="recording"))
        self.assertEqual(decision.rewind_action, RewindAction.FLASHBACK)
        applied = RecorderApplicationService().apply(
            decision, state, observation, current_setup=None,
            race_context=dict, finish_lap=lambda _: (False, "unused"),
            mark_session_recording=lambda: None,
        )
        self.assertEqual(applied.code, DecisionCode.FLASHBACK_CONTINUE_CAPTURE.value)
        self.assertEqual(len(state.samples), 1)
        self.assertFalse(state.invalid)
        self.assertEqual(state.events[-1]["pruned_sample_count"], 1)

    def test_grid_and_one_shot_qualifying_opening_packets_are_explicit(self):
        observation = LapDataObservation(packet(1, 100, 350), session_time=1.0)
        race = decide_lap_boundary(RecorderState(), observation, context(mode="race", session_type=15))
        qualifying = decide_lap_boundary(RecorderState(), observation, context(mode="qualifying", session_type=9))
        self.assertEqual(race.reasons, [ReasonCode.VERIFIED_RACE_GRID_START])
        self.assertEqual(qualifying.reasons, [ReasonCode.VERIFIED_ONE_SHOT_QUALIFYING_START])
        self.assertTrue(race.capture_new_attempt)
        self.assertTrue(qualifying.capture_new_attempt)

    def test_race_grid_capture_survives_negative_distance_timing_line_crossing(self):
        state = RecorderState(active_lap_number=1, capture_current_lap=True)
        state.lap_packet.update(packet(1, 4_850, -1.2, total_distance_m=-1.2))
        state.samples = [{"current_lap_ms": 2_000, "lap_distance_m": -70.0}]
        observation = LapDataObservation(
            packet(1, 4_888, 0.23, last_ms=0, total_distance_m=0.23),
            session_time=4.905,
        )

        decision = decide_lap_boundary(
            state, observation, context(status="recording", mode="race", session_type=15),
        )
        self.assertEqual(decision.code, DecisionCode.CONTINUE_CAPTURE)
        self.assertEqual(decision.reasons, [ReasonCode.PRESERVED_RACE_GRID_CROSSING])
        self.assertFalse(decision.begin_new_attempt)
        self.assertTrue(decision.flags["preserve_race_grid_crossing"])

        applied = RecorderApplicationService().apply(
            decision, state, observation, current_setup=None,
            race_context=dict, finish_lap=lambda _: (False, "unused"),
            mark_session_recording=lambda: None,
        )
        self.assertEqual(applied.code, DecisionCode.CONTINUE_CAPTURE.value)
        self.assertEqual(state.samples, [{"current_lap_ms": 2_000, "lap_distance_m": -70.0}])

    def test_effect_order_remains_finish_then_status_then_new_attempt(self):
        state = RecorderState(active_lap_number=1, capture_current_lap=True)
        state.lap_packet.update(packet(1, 90_000, 5885, total_distance_m=5885))
        observation = LapDataObservation(packet(2, 0, 0, 90_000, 5890))
        decision = decide_lap_boundary(state, observation, context())
        calls = []
        applied = RecorderApplicationService().apply(
            decision, state, observation, current_setup=None,
            race_context=lambda: calls.append("context") or {},
            finish_lap=lambda _: calls.append("finish") or (True, "finish_saved_test"),
            mark_session_recording=lambda: calls.append("mark"),
        )
        self.assertEqual(calls, ["finish", "mark", "context"])
        self.assertEqual(applied.code, "finish_saved_and_start_capture")
        self.assertEqual(state.lifecycle, RecorderLifecycle.CAPTURING)


class PacketDispatchOrderTests(unittest.TestCase):
    def test_specialized_lap_handling_precedes_full_packet_storage(self):
        calls = []

        class Store:
            def record_lap_state(self, *_args):
                calls.append("lap")

            def record_full_packet(self, *_args):
                calls.append("full")

            def record_receiver_error(self, _error):
                calls.append("error")

        header = SimpleNamespace(
            packet_id=PACKET_LAP_DATA, session_uid=7, session_time=1.0,
            frame_identifier=2, overall_frame_identifier=3,
        )
        receiver = UdpReceiver(Store())
        with (
            patch("src.f1telemetry.receiver.decode_header", return_value=header),
            patch("src.f1telemetry.receiver.supported_header", return_value=True),
            patch("src.f1telemetry.receiver.decode_player_lap_data", return_value=packet(1, 10, 1)),
            patch("src.f1telemetry.receiver.decode_packet", return_value={"player": {}}),
        ):
            receiver._handle(b"packet")
        self.assertEqual(calls, ["lap", "full"])
        self.assertTrue(receiver.daemon)
        self.assertEqual(receiver.name, "f1-udp-receiver")


if __name__ == "__main__":
    unittest.main()
