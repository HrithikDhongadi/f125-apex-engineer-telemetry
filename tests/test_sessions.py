import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from src.f1telemetry.receiver import LEGACY_SESSION_ID, SessionStore, UdpReceiver


UID = 1001


def game(uid=UID, mode="time_trial", track_id=7, track_length=5891):
    return {
        "session_uid": uid, "session_type": 18 if mode == "time_trial" else 15,
        "mode": mode, "track_id": track_id, "track_length_m": track_length,
    }


def lap_state(number, current_ms=0, distance=0, last_ms=0, invalid=False):
    return {
        "lap_number": number, "last_lap_ms": last_ms, "current_lap_ms": current_ms,
        "lap_distance_m": distance, "invalid": invalid, "sector1_ms": 28_000,
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

    def test_setup_is_snapshotted_at_lap_start(self):
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
            store.record_setup(second_setup, UID)
            for index in range(15, 30):
                store.record_telemetry(telemetry(), index, UID)
            store.record_lap_state(lap_state(2, 0, 0, 90_000), UID)
            captured = store.session_laps[run["id"]][0].setup
            self.assertEqual({key: captured[key] for key in first_setup}, first_setup)
            self.assertTrue(all(captured["_provenance"][key] == "decoded_udp" for key in first_setup))

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
            distances = [sample["lap_distance_m"] for sample in restarted.samples]
            self.assertLessEqual(min(distances), 5)
            self.assertGreater(max(distances), 5_800)
            self.assertNotIn(90, [sample["speed_kph"] for sample in restarted.samples])

    def test_midlap_flashback_discards_lap_until_next_crossing(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_lap_state(lap_state(3), UID)
            run = store.start_time_trial_run("flashback")
            for index in range(30):
                store.record_lap_state(lap_state(3, 20_000 + index * 500, 1500 + index * 20), UID)
                store.record_telemetry(telemetry(), index, UID)
            store.record_lap_state(lap_state(3, 15_000, 1100), UID)
            store.record_lap_state(lap_state(4, 0, 0, 90_000), UID)
            fill_lap(store, UID, 4)
            self.assertEqual([lap.number for lap in store.session_laps[run["id"]]], [4])

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
