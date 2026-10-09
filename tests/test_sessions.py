import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.f1telemetry.circuit_profiles import CircuitProfileError
from src.f1telemetry.receiver import LEGACY_SESSION_ID, SessionStore, UdpReceiver


UID = 1001


def game(uid=UID, mode="time_trial", track_id=7, track_length=5891):
    return {
        "session_uid": uid, "session_type": 18 if mode == "time_trial" else 15,
        "mode": mode, "track_id": track_id, "track_length_m": track_length,
    }


def lap_state(number, current_ms=0, distance=0, last_ms=0, invalid=False, total_distance=None):
    return {
        "lap_number": number, "last_lap_ms": last_ms, "current_lap_ms": current_ms,
        "lap_distance_m": distance, "total_distance_m": distance if total_distance is None else total_distance,
        "invalid": invalid, "sector1_ms": 28_000,
        "sector2_ms": 39_000,
    }


def telemetry(speed=200):
    return {
        "speed_kph": speed, "throttle": 100, "brake": 0, "steering": 0, "gear": 7,
        "brake_temps_c": [400, 410, 420, 430], "tyre_inner_c": [90, 91, 92, 93],
    }


def fill_lap(store, uid, number, time_ms=90_000):
    for index in range(30):
        current = int(index / 29 * (time_ms - 500))
        distance = index / 29 * 5880
        store.record_lap_state(lap_state(number, current, distance), uid)
        store.record_telemetry(telemetry(180 + index), current / 1000, uid)
    store.record_lap_state(lap_state(number + 1, 0, 0, time_ms), uid)


class RecordingSessionTests(unittest.TestCase):
    def test_packet_failure_is_reported_without_escaping_receiver_loop(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            receiver = UdpReceiver(store)
            with patch.object(receiver, "_handle", side_effect=OverflowError("test packet")):
                self.assertFalse(receiver._handle_safely(b"packet"))
            self.assertEqual(store.latest["receiver_error_count"], 1)
            self.assertEqual(store.latest["receiver_error"], "OverflowError: test packet")

    def test_closed_session_deletion_is_recoverable_and_preserves_pb_and_note(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game())
            store.record_lap_state(lap_state(1, 40_000, 2_500), UID)
            run = store.start_time_trial_run("delete me")
            store.record_lap_state(lap_state(2, 0, 0, 90_000), UID)
            fill_lap(store, UID, 2, 89_000)
            lap = store.session_laps[run["id"]][0]
            store.set_note(lap.id, "keep this note")
            pb_path = root / "personal_bests" / "track-7__time_trial.json"
            self.assertTrue(pb_path.exists())

            with self.assertRaisesRegex(ValueError, "explicitly confirmed"):
                store.delete_session(run["id"])
            with self.assertRaisesRegex(ValueError, "Stop the active recording"):
                store.delete_session(run["id"], True)
            store.stop_recording()
            result = store.delete_session(run["id"], True)

            self.assertTrue(result["recoverable"])
            self.assertTrue(result["personal_bests_preserved"])
            self.assertTrue(result["notes_preserved"])
            self.assertFalse((root / "sessions" / run["id"]).exists())
            trashed = root / result["moved_to"]
            self.assertTrue((trashed / "session.json").exists())
            deletion = json.loads((trashed / "deletion.json").read_text())
            self.assertEqual(deletion["session"]["id"], run["id"])
            self.assertIn(lap.id, deletion["lap_ids"])
            self.assertNotIn(run["id"], store.sessions)
            self.assertIsNone(store.get_lap_object(lap.id))
            self.assertEqual(store.selected_session_id, LEGACY_SESSION_ID)
            self.assertTrue(pb_path.exists())
            self.assertEqual(store.notes[lap.id], "keep this note")

            reloaded = SessionStore(root)
            self.assertNotIn(run["id"], reloaded.sessions)
            self.assertIsNotNone(reloaded.personal_best("track-7__time_trial"))
            self.assertEqual(reloaded.notes[lap.id], "keep this note")

    def test_legacy_session_cannot_be_deleted(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            with self.assertRaisesRegex(ValueError, "Legacy captures cannot be deleted"):
                store.delete_session(LEGACY_SESSION_ID, True)

    def test_deleted_session_can_be_listed_and_restored_without_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game())
            store.record_lap_state(lap_state(1, 40_000, 2_500), UID)
            run = store.start_time_trial_run("restore me")
            store.record_lap_state(lap_state(2, 0, 0, 90_000), UID)
            fill_lap(store, UID, 2)
            store.stop_recording()
            deleted = store.delete_session(run["id"], True)
            entries = store.list_deleted_sessions()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["session_id"], run["id"])
            self.assertIsNotNone(entries[0]["purge_at"])

            restored = store.restore_deleted_session(entries[0]["trash_id"], True)
            self.assertEqual(restored["restored_session_id"], run["id"])
            self.assertEqual(restored["lap_count"], 1)
            self.assertIn(run["id"], store.sessions)
            self.assertTrue((root / "sessions" / run["id"] / "deletion.json").exists())
            self.assertFalse((root / deleted["moved_to"]).exists())

    def test_expired_deleted_session_is_permanently_purged_by_retention_setting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game())
            run = store.start_time_trial_run("expire me")
            store.stop_recording()
            deleted = store.delete_session(run["id"], True)
            trash = root / deleted["moved_to"]
            record = json.loads((trash / "deletion.json").read_text())
            record["deleted_at"] = "2020-01-01T00:00:00Z"
            (trash / "deletion.json").write_text(json.dumps(record))

            result = store.update_settings({"trash_retention_days": 1})
            self.assertIn(trash.name, result["purge"]["purged"])
            self.assertFalse(trash.exists())

    def test_zero_retention_keeps_deleted_sessions_until_restored(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            saved = store.update_settings({"trash_retention_days": 0})
            self.assertEqual(saved["settings"]["trash_retention_days"], 0)
            self.assertEqual(saved["restart_required"], [])

    def test_opt_in_diagnostic_records_staggered_boundary_decisions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            self.assertFalse(store.configure_diagnostics(True)["active_file"])
            store.record_game_session(game())
            store.record_lap_state(lap_state(23, 89_000, 5_885), UID)
            run = store.start_time_trial_run("diagnostic reproduction")

            # Reproduce the suspected ordering without changing recorder logic:
            # lap number advances while time/distance still describe the old lap,
            # then time/distance reset on a same-number packet.
            store.record_lap_state(lap_state(24, 89_020, 5_889, 88_900, total_distance=176_718.05), UID, {
                "session_time": 4_000.0, "frame_identifier": 100,
                "overall_frame_identifier": 200,
            })
            store.record_lap_state(lap_state(24, 16, 1.2, 88_900, total_distance=176_721.52), UID, {
                "session_time": 4_000.05, "frame_identifier": 101,
                "overall_frame_identifier": 201,
            })

            diagnostic_dir = root / "sessions" / run["id"] / "diagnostics"
            decisions = [
                json.loads(row) for row in
                (diagnostic_dir / "lap-data-00-run-start.jsonl").read_text().splitlines()
                if json.loads(row).get("event") == "lap_data_decision"
            ]
            first, second = decisions[-2:]
            self.assertEqual(first["decision"], "rejected_crossing")
            self.assertIn("rejected_crossing_lap_number_advanced_but_time_or_distance_not_near_line", first["reasons"])
            self.assertEqual(first["previous_packet"]["lap_number"], 23)
            self.assertEqual(first["packet"]["lap_number"], 24)
            self.assertEqual(first["frame_identifier"], 100)
            self.assertEqual(first["overall_frame_identifier"], 200)
            self.assertEqual(first["before"]["recording_status"], "armed")
            self.assertFalse(first["after"]["capture_current_lap"])
            self.assertEqual(second["packet"]["total_distance_m"], 176_721.52)
            self.assertEqual(second["decision"], "start_capture")
            self.assertIn("verified_start_same_number_wrap", second["reasons"])
            self.assertEqual(store.sessions[run["id"]].status, "recording")

    def test_live_zero_timer_same_number_sequence_saves_first_and_following_laps(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(track_length=5_890))

            # Replay the decisive packets from session-20261004T132600-dbdce8e9.
            # F1 25 called the untimed approach lap 31 and held its timer at
            # zero, then crossed the line without changing the lap number.
            store.record_lap_state(
                lap_state(31, 0, 130.77, 88_741, total_distance=170_960.30), UID,
                {"session_time": 5_463.186523, "frame_identifier": 109_838,
                 "overall_frame_identifier": 110_159},
            )
            run = store.start_time_trial_run("live sequence replay")
            self.assertEqual(store.sessions[run["id"]].status, "armed")
            store.record_lap_state(
                lap_state(31, 0, 5_888.52, 88_741, total_distance=176_718.05), UID,
                {"session_time": 5_489.737793, "frame_identifier": 110_372,
                 "overall_frame_identifier": 110_693},
            )
            store.record_lap_state(
                lap_state(31, 16, 1.32, 88_741, total_distance=176_721.52), UID,
                {"session_time": 5_489.787598, "frame_identifier": 110_373,
                 "overall_frame_identifier": 110_694},
            )
            self.assertEqual(store.sessions[run["id"]].status, "recording")
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.active_lap_number, 31)

            lap31_start_total = 176_721.52
            for index in range(40):
                progress = index / 39
                current = int(16 + progress * (89_024 - 16))
                distance = 1.32 + progress * (5_887.82 - 1.32)
                store.record_lap_state(lap_state(
                    31, current, distance, 88_741,
                    total_distance=lap31_start_total + distance - 1.32,
                ), UID)
                store.record_telemetry(telemetry(180 + index), 5_489.788 + current / 1_000, UID)
            store.record_lap_state(
                lap_state(32, 0, 0.5, 89_064, total_distance=182_611.38), UID
            )

            lap32_start_total = 182_611.38
            for index in range(40):
                progress = index / 39
                current = int(progress * 89_160)
                distance = 0.5 + progress * (5_888.6 - 0.5)
                store.record_lap_state(lap_state(
                    32, current, distance, 89_064,
                    total_distance=lap32_start_total + distance - 0.5,
                ), UID)
                store.record_telemetry(telemetry(185 + index), 5_578.843 + current / 1_000, UID)
            store.record_lap_state(
                lap_state(33, 0, 0.6, 89_192, total_distance=188_502.8), UID
            )

            recorded = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in recorded], [31, 32])
            self.assertEqual([lap.time_ms for lap in recorded], [89_064, 89_192])
            for lap in recorded:
                self.assertLessEqual(lap.samples[0]["lap_distance_m"], 1.32)
                self.assertGreaterEqual(lap.samples[-1]["lap_distance_m"], 5_887.82)

    def test_live_zero_to_zero_timer_wrap_starts_first_lap_40(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(track_length=5_890))
            store.record_lap_state(lap_state(
                40, 0, 5_888.40, 89_021, total_distance=229_733.98,
            ), UID)
            run = store.start_time_trial_run("live lap 40 replay")
            store.record_lap_state(lap_state(
                40, 0, 1.08, 89_021, total_distance=229_737.34,
            ), UID, {"session_time": 7_217.525879, "frame_identifier": 145_222})
            self.assertEqual(store.sessions[run["id"]].status, "recording")
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.active_lap_number, 40)

            for index in range(40):
                progress = index / 39
                store.record_lap_state(lap_state(
                    40, int(progress * 91_188), 1.08 + progress * (5_887.4 - 1.08),
                    invalid=True, total_distance=229_737.34 + progress * 5_886.32,
                ), UID)
                store.record_telemetry(telemetry(), 7_217.526 + progress * 91.188, UID)
            store.record_lap_state(lap_state(
                41, 0, 0.18, 91_238, total_distance=235_627.11,
            ), UID)

            captured = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in captured], [40])
            self.assertEqual(captured[0].time_ms, 91_238)
            self.assertTrue(captured[0].invalid)

    def test_zero_timer_same_number_rewind_to_start_begins_restarted_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(track_length=5_890))
            store.record_lap_state(
                lap_state(31, 0, 5_888.52, total_distance=176_718.05), UID
            )
            run = store.start_time_trial_run("flashback guard")

            # Backward total distance proves this is not a natural crossing,
            # but landing just after the line is a clean restarted-lap start.
            store.record_lap_state(
                lap_state(31, 16, 1.32, total_distance=170_831.0), UID
            )
            self.assertEqual(store.sessions[run["id"]].status, "recording")
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.active_lap_events[0]["type"], "restart_lap")
            self.assertEqual(store.session_laps[run["id"]], [])

    def test_diagnostic_is_opt_in_bounded_and_restart_uses_separate_segment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game())
            store.record_lap_state(lap_state(8), UID)
            run = store.start_time_trial_run("no diagnostic")
            self.assertFalse((root / "sessions" / run["id"] / "diagnostics").exists())
            store.stop_recording()

            store.configure_diagnostics(True)
            second = store.start_time_trial_run("diagnostic restart")
            store.record_lap_state(lap_state(9, 30_000, 2_000), UID)
            store.record_lap_state(lap_state(8, 0, -50), UID)
            diagnostic_dir = root / "sessions" / second["id"] / "diagnostics"
            self.assertTrue((diagnostic_dir / "lap-data-00-run-start.jsonl").exists())
            restart_path = diagnostic_dir / "lap-data-01-restart-lap.jsonl"
            self.assertTrue(restart_path.exists())
            restart = json.loads(restart_path.read_text().splitlines()[0])
            self.assertEqual(restart["decision"], "discard_partial_lap")
            self.assertIn("discard_lap_number_decreased_probable_restart_or_flashback", restart["reasons"])

        with TemporaryDirectory() as directory, patch("src.f1telemetry.receiver.MAX_DIAGNOSTIC_EVENTS", 2):
            root = Path(directory)
            store = SessionStore(root)
            store.configure_diagnostics(True)
            store.record_game_session(game())
            store.record_lap_state(lap_state(1, 20_000, 1_000), UID)
            store.start_time_trial_run("bounded")
            store.record_lap_state(lap_state(1, 20_050, 1_003), UID)
            store.record_lap_state(lap_state(1, 20_100, 1_006), UID)
            status = store.snapshot()["diagnostics"]
            self.assertTrue(status["truncated"])
            rows = (root / status["active_file"]).read_text().splitlines()
            self.assertEqual(json.loads(rows[-1])["event"], "diagnostic_limit_reached")

    def test_time_trial_start_mid_lap_arms_until_next_full_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(1, 40_000, 2500), UID)
            started = store.start_time_trial_run("18/18 baseline")
            self.assertEqual(started["status"], "armed")
            store.record_lap_state(lap_state(2, 0, 0, 90_000), UID)
            self.assertEqual(store.sessions[started["id"]].status, "recording")
            fill_lap(store, UID, 2)
            self.assertEqual([lap.number for lap in store.session_laps[started["id"]]], [2])

    def test_time_trial_started_just_before_line_excludes_lap_19_and_saves_full_lap_20(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())

            # The HUD still says lap 19 on the approach to the line. Starting
            # here must arm the run, not claim this partial lap as complete.
            store.record_lap_state(lap_state(19, 89_000, 5_885), UID)
            run = store.start_time_trial_run("just behind the line")
            self.assertEqual(run["status"], "armed")
            store.record_telemetry(telemetry(90), 100.0, UID)
            self.assertEqual(store.active_samples, [])

            # At the line the game changes to lap 20. Capture starts under that
            # actual game lap number; lap 19 is neither saved nor synthesized.
            store.record_lap_state(lap_state(20, 0, 0.45, 89_000), UID)
            snapshot = store.snapshot()
            self.assertEqual(snapshot["recording"]["status"], "recording")
            self.assertEqual(snapshot["recording"]["capturing_lap_number"], 20)

            for index in range(30):
                progress = index / 29
                current = int(progress * (89_242 - 60))
                distance = 0.45 + progress * (5_887.44 - 0.45)
                store.record_lap_state(lap_state(20, current, distance), UID)
                store.record_telemetry(telemetry(180 + index), 101 + progress * 89.182, UID)
            store.record_lap_state(lap_state(21, 0, 0, 89_242), UID)

            recorded = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in recorded], [20])
            self.assertEqual(recorded[0].time_ms, 89_242)
            self.assertAlmostEqual(recorded[0].samples[0]["lap_distance_m"], 0.45)
            self.assertAlmostEqual(recorded[0].samples[-1]["lap_distance_m"], 5_887.44)
            self.assertNotIn(90, [sample["speed_kph"] for sample in recorded[0].samples])

    def test_stop_discards_only_partial_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(1), UID)
            run = store.start_time_trial_run("test")
            fill_lap(store, UID, 1)
            for index in range(10):
                store.record_telemetry(telemetry(), 91 + index, UID)
            stopped = store.stop_recording()
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(len(store.session_laps[run["id"]]), 1)
            self.assertEqual(len(list((Path(directory) / "sessions" / run["id"]).glob("lap-*.json"))), 1)

    def test_two_runs_under_same_uid_remain_separate(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(1), UID)
            first = store.start_time_trial_run("first")
            fill_lap(store, UID, 1)
            store.stop_recording()
            second = store.start_time_trial_run("second")
            fill_lap(store, UID, 2)
            store.stop_recording()
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(len(store.session_laps[first["id"]]), 1)
            self.assertEqual(len(store.session_laps[second["id"]]), 1)

    def test_race_auto_start_manual_stop_does_not_reopen_same_uid(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(mode="race"))
            active = store.active_recording_id
            self.assertIsNotNone(active)
            store.stop_recording()
            store.record_game_session(game(mode="race"))
            self.assertIsNone(store.active_recording_id)
            self.assertEqual(store.sessions[active].status, "stopped")

    def test_race_finish_uid_glitch_does_not_create_empty_duplicate(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(mode="race"))
            race_id = store.active_recording_id

            # Observed live finish ordering: a transient UID closes the real
            # race, then 16 ms later a complete packet bundle arrives using
            # the just-completed race UID, beginning with Session data.
            store.record_telemetry(telemetry(), 557.620, UID + 99)
            self.assertIsNone(store.active_recording_id)
            self.assertEqual(store.sessions[race_id].status, "completed")

            store.record_game_session(game(uid=UID, mode="race"))
            self.assertIsNone(store.active_recording_id)
            self.assertEqual(
                [session.id for session in store.sessions.values() if session.mode == "race"],
                [race_id],
            )

            # The guard is specific to the completed UID; a genuinely new
            # game session still starts its own race immediately.
            store.record_game_session(game(uid=UID + 200, mode="race"))
            self.assertIsNotNone(store.active_recording_id)
            self.assertNotEqual(store.active_recording_id, race_id)

    def test_profile_deletion_clears_session_reference_and_is_recoverable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game(mode="race", track_id=10, track_length=7_004))
            session_id = store.active_recording_id
            profile = store.save_profile({
                "id": "spa-editable-v1", "packet_format": 2025, "track_id": 10,
                "layout": "normal", "measured_game_length_m": 7_004,
                "turns": [{"number": 1, "entry_m": 50, "estimated_apex_m": 100, "exit_m": 150}],
            })
            store.select_profile(session_id, profile["id"])
            with self.assertRaisesRegex(CircuitProfileError, "explicitly confirmed"):
                store.delete_profile(profile["id"])
            result = store.delete_profile(profile["id"], True)
            self.assertEqual(result["cleared_session_ids"], [session_id])
            self.assertIsNone(store.sessions[session_id].circuit_profile_id)
            self.assertTrue((root / result["moved_to"]).exists())

    def test_live_race_grid_packet_captures_lap_one_from_nonzero_start_position(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(mode="race", track_length=5_890))
            run_id = store.active_recording_id
            self.assertEqual(store.sessions[run_id].status, "armed")

            # Exact opening state from the live race: P14 started 25.83 m
            # beyond the lap-distance origin, but session time proves this is
            # the race start rather than a recording begun mid-lap.
            store.record_lap_state(lap_state(
                1, 0, 25.83, 0, total_distance=25.83,
            ), UID, {"session_time": 0.0, "frame_identifier": 0})
            self.assertEqual(store.sessions[run_id].status, "recording")
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.active_lap_number, 1)

            for index in range(40):
                progress = index / 39
                current = int(progress * 100_047)
                distance = 25.83 + progress * (5_888.78 - 25.83)
                store.record_lap_state(lap_state(
                    1, current, distance, total_distance=distance,
                ), UID)
                store.record_telemetry(telemetry(160 + index), progress * 100.047, UID)
            store.record_lap_state(lap_state(
                2, 16, 1.49, 100_077, total_distance=5_892.16,
            ), UID)

            laps = store.session_laps[run_id]
            self.assertEqual([lap.number for lap in laps], [1])
            self.assertEqual(laps[0].time_ms, 100_077)
            self.assertAlmostEqual(laps[0].samples[0]["lap_distance_m"], 25.83)
            self.assertGreater(laps[0].samples[-1]["lap_distance_m"], 5_880)

    def test_new_uid_closes_and_resets_without_mixing_samples(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            first = store.start_time_trial_run("first")
            store.record_lap_state(lap_state(1), UID)
            store.record_telemetry(telemetry(), 1.0, UID)
            store.record_game_session(game(uid=2002))
            self.assertEqual(store.sessions[first["id"]].status, "completed")
            self.assertEqual(store.session_laps[first["id"]], [])
            self.assertIsNone(store.active_lap_number)
            second = store.start_time_trial_run("second")
            fill_lap(store, 2002, 1)
            self.assertTrue(all(lap.game_session_uid == 2002 for lap in store.session_laps[second["id"]]))

    def test_restart_marks_open_session_interrupted(self):
        with TemporaryDirectory() as directory:
            first_store = SessionStore(Path(directory))
            first_store.record_game_session(game())
            run = first_store.start_time_trial_run("unfinished")
            reloaded = SessionStore(Path(directory))
            self.assertEqual(reloaded.sessions[run["id"]].status, "interrupted")
            metadata = json.loads((Path(directory) / "sessions" / run["id"] / "session.json").read_text())
            self.assertEqual(metadata["status"], "interrupted")

    def test_legacy_capture_remains_unmodified(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lap-20250101T000000Z-1.json"
            original = b'{"number":1,"time_ms":90000,"invalid":false,"samples":[],"saved_at":"20250101T000000Z"}'
            path.write_bytes(original)
            store = SessionStore(Path(directory))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(store.sessions[LEGACY_SESSION_ID].status, "legacy")
            self.assertEqual(len(store.session_laps[LEGACY_SESSION_ID]), 1)

    def test_old_four_field_setup_loads_without_inventing_missing_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lap-20250101T000000Z-1.json"
            payload = {
                "number": 1, "time_ms": 90_000, "invalid": False, "samples": [],
                "saved_at": "20250101T000000Z",
                "setup": {"front_wing": 19, "rear_wing": 17, "on_throttle_diff": 20, "off_throttle_diff": 45},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            lap = SessionStore(Path(directory)).session_laps[LEGACY_SESSION_ID][0]
            self.assertEqual(lap.setup, payload["setup"])
            self.assertNotIn("front_camber", lap.setup)

    def test_setup_refreshes_during_opening_window_then_is_frozen(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            first_setup = {"front_wing": 18, "rear_wing": 18, "on_throttle_diff": 25, "off_throttle_diff": 50}
            second_setup = {"front_wing": 20, "rear_wing": 19, "on_throttle_diff": 30, "off_throttle_diff": 55}
            store.record_setup(first_setup, UID)
            store.record_lap_state(lap_state(1), UID)
            run = store.start_time_trial_run("setup test")
            for index in range(15):
                store.record_telemetry(telemetry(), index, UID)
            store.record_setup(second_setup, UID, {"session_time": 0.5, "frame_identifier": 90})
            for index in range(15, 30):
                store.record_telemetry(telemetry(), index, UID)
            store.record_lap_state(lap_state(2, 0, 0, 90_000), UID)
            captured = store.session_laps[run["id"]][0].setup
            self.assertEqual({key: captured[key] for key in second_setup}, second_setup)
            self.assertEqual(captured["_capture"]["frame_identifier"], 90)
            self.assertTrue(all(captured["_provenance"][key] == "decoded_udp" for key in second_setup))

    def test_historical_setup_amendment_backs_up_and_syncs_pb(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session(game())
            original_setup = {"front_wing": 19, "rear_wing": 17, "on_throttle_diff": 20, "off_throttle_diff": 45}
            store.record_setup(original_setup, UID)
            store.record_lap_state(lap_state(18), UID)
            run = store.start_time_trial_run("Silverstone PB")
            fill_lap(store, UID, 18, 88_617)
            lap = store.session_laps[run["id"]][0]
            lap_path = root / "sessions" / run["id"] / f"{lap.id}.json"
            original_bytes = lap_path.read_bytes()
            original_payload = json.loads(original_bytes)
            result = store.amend_lap_setup(lap.id, {
                "front_camber": -3.5, "rear_camber": -2.0, "front_left_tyre_pressure_psi": 24.0,
                "front_right_tyre_pressure_psi": 24.0, "rear_left_tyre_pressure_psi": 21.1,
                "rear_right_tyre_pressure_psi": 21.1, "fuel_load_kg": 5.0,
            })
            amended = json.loads(lap_path.read_text())
            audit = root / result["audit_record"]
            self.assertEqual((audit / "original-lap.json").read_bytes(), original_bytes)
            self.assertEqual(amended["samples"], original_payload["samples"])
            self.assertEqual(amended["time_ms"], 88_617)
            self.assertEqual(amended["recording_session_id"], run["id"])
            self.assertEqual(amended["setup"]["front_wing"], 19)
            self.assertEqual(amended["setup"]["_provenance"]["front_wing"], "decoded_udp")
            self.assertEqual(amended["setup"]["_provenance"]["front_camber"], "manual")
            self.assertTrue(result["pb_synced"])
            pb = store.personal_best("track-7__time_trial")
            self.assertEqual(pb["setup"], amended["setup"])
            self.assertEqual(pb["lap"]["setup"], amended["setup"])

    def test_unknown_track_display_override_preserves_numeric_identity(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(track_id=99))
            store.record_lap_state(lap_state(1), UID)
            run = store.start_time_trial_run("unknown circuit")
            self.assertEqual(store.sessions[run["id"]].summary([])["track_name"], "Unknown track (ID 99)")
            summary = store.override_track_name(run["id"], "My local circuit")
            self.assertEqual(summary["track_name"], "My local circuit")
            self.assertEqual(summary["track_id"], 99)

    def test_restart_lap_with_zero_last_time_captures_next_full_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(8), UID)
            run = store.start_time_trial_run("restart regression")
            fill_lap(store, UID, 8)
            store.record_lap_state(lap_state(9, 35_000, 2300), UID)
            for index in range(8):
                store.record_telemetry(telemetry(90), 100 + index, UID)

            # Restart Lap rewinds behind the line. The abandoned samples must
            # be discarded, and the next crossing has no completed-lap time.
            store.record_lap_state(lap_state(8, 0, -60, 0), UID)
            store.record_lap_state(lap_state(9, 100, 5, 0), UID)
            self.assertEqual(store.snapshot()["recording"]["capturing_lap_number"], 9)
            fill_lap(store, UID, 9)

            recorded = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in recorded], [8, 9])
            restarted = recorded[-1]
            self.assertEqual(restarted.events[0]["type"], "restart_lap")
            distances = [sample["lap_distance_m"] for sample in restarted.samples]
            self.assertLessEqual(min(distances), 5)
            self.assertGreater(max(distances), 5_800)
            self.assertNotIn(90, [sample["speed_kph"] for sample in restarted.samples])

    def test_midlap_flashback_prunes_superseded_branch_and_saves_invalid_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(3), UID)
            run = store.start_time_trial_run("flashback")
            for index in range(30):
                store.record_lap_state(lap_state(3, index * 1_500, index * 100, invalid=index > 20), UID)
                store.record_telemetry(telemetry(90), index, UID)
            # Exact rewind observed on live lap 35: frame 122938 replaced the
            # future branch ending at 46.327 s / 2,852.61 m.
            store.record_lap_state(lap_state(3, 46_327, 2_852.61, invalid=True), UID)
            store.record_telemetry(telemetry(90), 30, UID)
            store.record_lap_state(lap_state(3, 33_549, 2_113.72, invalid=True), UID, {
                "session_time": 6_114.949219, "frame_identifier": 122_938,
            })
            self.assertTrue(store.capture_current_lap)
            self.assertTrue(store.active_samples)
            self.assertLessEqual(max(sample["lap_distance_m"] for sample in store.active_samples), 2_113.72)
            for index in range(40):
                progress = index / 39
                current = int(33_549 + progress * (92_168 - 33_549))
                distance = 2_113.72 + progress * (5_889.42 - 2_113.72)
                store.record_lap_state(lap_state(3, current, distance, invalid=True), UID)
                store.record_telemetry(telemetry(180 + index), 46 + index, UID)
            store.record_lap_state(lap_state(4, 16, 2.17, 92_192), UID)
            fill_lap(store, UID, 4)

            recorded = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in recorded], [3, 4])
            flashed = recorded[0]
            self.assertEqual(flashed.time_ms, 92_192)
            self.assertTrue(flashed.invalid)
            self.assertEqual(flashed.events[0]["type"], "flashback")
            self.assertGreater(flashed.events[0]["pruned_sample_count"], 0)
            distances = [sample["lap_distance_m"] for sample in flashed.samples]
            lap_times = [sample["current_lap_ms"] for sample in flashed.samples]
            self.assertTrue(all(b >= a for a, b in zip(distances, distances[1:])))
            self.assertTrue(all(b >= a for a, b in zip(lap_times, lap_times[1:])))

    def test_race_flashback_lap_uses_game_validity_and_is_saved(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(mode="race"))
            run_id = store.active_recording_id
            store.record_lap_state(lap_state(4, 89_000, 5_880), UID)
            store.record_lap_state(lap_state(5), UID)
            for index in range(30):
                store.record_lap_state(lap_state(5, index * 1_000, index * 80), UID)
                store.record_telemetry(telemetry(), index, UID)
            # The abandoned future branch was game-invalid. After rewind the
            # game clears its flag, so the recorder must not carry invented
            # invalidity into the replacement timeline.
            store.record_lap_state(lap_state(5, 30_000, 2_400, invalid=True), UID)
            store.record_lap_state(lap_state(5, 15_000, 1_200, invalid=False), UID)
            for index in range(40):
                progress = index / 39
                store.record_lap_state(lap_state(
                    5, int(15_000 + progress * 75_000), 1_200 + progress * 4_680,
                    invalid=False,
                ), UID)
                store.record_telemetry(telemetry(), 31 + index, UID)
            store.record_lap_state(lap_state(6, 0, 0, 90_000, invalid=False), UID)

            raced = store.session_laps[run_id][0]
            self.assertFalse(raced.invalid)
            self.assertEqual(raced.events[0]["type"], "flashback")

    def test_restart_reset_at_start_immediately_recaptures_same_lap_number(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(38), UID)
            run = store.start_time_trial_run("restart at line")
            for index in range(30):
                store.record_lap_state(lap_state(38, index * 1_500, index * 100), UID)
                store.record_telemetry(telemetry(90), index, UID)

            # F1 25 can place the car directly at/just after the line without
            # emitting a separate negative-distance crossing packet.
            store.record_lap_state(lap_state(
                38, 0, 0.5, 0, total_distance=0.5,
            ), UID, {"session_time": 45.0, "frame_identifier": 1_000})
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.active_lap_number, 38)
            self.assertEqual(store.active_samples, [])
            self.assertEqual(store.active_lap_events[0]["type"], "restart_lap")

            for index in range(40):
                progress = index / 39
                store.record_lap_state(lap_state(
                    38, int(progress * 90_000), 0.5 + progress * 5_879.5,
                    invalid=False, total_distance=0.5 + progress * 5_879.5,
                ), UID)
                store.record_telemetry(telemetry(180 + index), 46 + index, UID)
            store.record_lap_state(lap_state(39, 0, 0, 90_000), UID)

            restarted = store.session_laps[run["id"]][0]
            self.assertEqual(restarted.number, 38)
            self.assertEqual(restarted.events[0]["type"], "restart_lap")
            self.assertNotIn(90, [sample["speed_kph"] for sample in restarted.samples])

    def test_live_restart_relocation_saves_only_the_real_restarted_lap_42(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game(track_length=5_890))
            store.record_lap_state(lap_state(42), UID)
            run = store.start_time_trial_run("live restart relocation replay")

            for index in range(40):
                progress = index / 39
                store.record_lap_state(lap_state(
                    42, int(progress * 20_053), progress * 1_178.81,
                    total_distance=241_518.30 + progress * 1_178.12,
                ), UID)
                store.record_telemetry(telemetry(90), 7_399.907 + progress * 20.053, UID)

            # Exact two-packet Restart Lap transition from the live run: the
            # timer resets first, then total distance reveals the relocation
            # to an untimed approach even though lap distance moves forward.
            store.record_lap_state(lap_state(
                42, 0, 1_180.91, 91_132, total_distance=242_698.52,
            ), UID, {"session_time": 7_420.009766, "frame_identifier": 149_276})
            store.record_lap_state(lap_state(
                42, 0, 4_823.54, 91_132, total_distance=240_450.47,
            ), UID, {"session_time": 7_420.060059, "frame_identifier": 149_277})
            self.assertFalse(store.capture_current_lap)
            self.assertEqual(store.active_samples, [])
            self.assertEqual(store.pending_restart_event["type"], "restart_lap")

            for index in range(20):
                progress = index / 19
                store.record_lap_state(lap_state(
                    42, 0, 4_823.54 + progress * (5_890.39 - 4_823.54), 91_132,
                    total_distance=240_450.47 + progress * (241_517.33 - 240_450.47),
                ), UID)
                store.record_telemetry(telemetry(95), 7_420.06 + index, UID)
            store.record_lap_state(lap_state(
                42, 33, 3.17, 91_132, total_distance=241_520.78,
            ), UID, {"session_time": 7_438.961914, "frame_identifier": 149_659})
            self.assertTrue(store.capture_current_lap)
            self.assertEqual(store.session_laps[run["id"]], [])

            for index in range(40):
                progress = index / 39
                store.record_lap_state(lap_state(
                    42, int(33 + progress * (89_430 - 33)), 3.17 + progress * (5_890.47 - 3.17),
                    total_distance=241_520.78 + progress * (247_408.08 - 241_520.78),
                ), UID)
                store.record_telemetry(telemetry(180 + index), 7_438.962 + progress * 89.405, UID)
            store.record_lap_state(lap_state(
                43, 33, 3.28, 89_441, total_distance=247_411.56,
            ), UID)

            captured = store.session_laps[run["id"]]
            self.assertEqual([lap.number for lap in captured], [42])
            self.assertEqual(captured[0].time_ms, 89_441)
            self.assertEqual(captured[0].events[0]["type"], "restart_lap")
            self.assertGreaterEqual(captured[0].samples[-1]["lap_distance_m"], 5_890)
            self.assertNotIn(90, [sample["speed_kph"] for sample in captured[0].samples])

    def test_start_before_first_lap_packet_cannot_save_partial_lap(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            run = store.start_time_trial_run("early start")
            self.assertEqual(run["status"], "armed")
            store.record_lap_state(lap_state(5, 42_000, 2800), UID)
            for index in range(30):
                store.record_telemetry(telemetry(), index, UID)
            store.record_lap_state(lap_state(6, 0, 0, 90_000), UID)
            fill_lap(store, UID, 6)
            self.assertEqual([lap.number for lap in store.session_laps[run["id"]]], [6])

    def test_packet_gap_midlap_is_not_treated_as_start_line(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            run = store.start_time_trial_run("packet gap")
            store.record_lap_state(lap_state(2, 25_000, 1700), UID)
            store.record_lap_state(lap_state(3, 30_000, 2100, 0), UID)
            self.assertEqual(store.sessions[run["id"]].status, "armed")
            self.assertFalse(store.capture_current_lap)

    def test_negative_distance_crossing_starts_capture_without_lap_number_change(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            run = store.start_time_trial_run("negative start")
            store.record_lap_state(lap_state(8, 0, -40, 0), UID)
            store.record_lap_state(lap_state(8, 100, 5, 0), UID)
            self.assertEqual(store.sessions[run["id"]].status, "recording")
            self.assertTrue(store.capture_current_lap)

    def test_auto_recording_does_not_replace_historical_selection(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.select_session(LEGACY_SESSION_ID)
            store.record_game_session(game(mode="race"))
            self.assertIsNotNone(store.active_recording_id)
            self.assertEqual(store.selected_session_id, LEGACY_SESSION_ID)

    def test_synthetic_udp_session_packet_auto_starts_race(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            receiver = UdpReceiver(store)
            header = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 1, UID, 0, 0, 0, 0, 255)
            body = bytearray(8)
            struct.pack_into("<H", body, 4, 5891)
            body[6] = 15
            struct.pack_into("<b", body, 7, 7)
            receiver._handle(header + body)
            self.assertEqual(store.game_mode, "race")
            self.assertEqual(store.track_id, 7)
            self.assertIsNotNone(store.active_recording_id)


if __name__ == "__main__":
    unittest.main()
