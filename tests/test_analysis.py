import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.f1telemetry.analysis import ComparisonError, aligned_trace, compare_laps
from src.f1telemetry.receiver import Lap, SessionStore


def sample(distance, lap_ms, speed=200):
    return {
        "t": lap_ms / 1000,
        "lap_distance_m": distance,
        "current_lap_ms": lap_ms,
        "speed_kph": speed,
        "throttle": 100,
        "brake": 0,
        "steering": 0,
        "gear": 7,
        "brake_temps_c": [400, 410, 420, 430],
        "tyre_inner_c": [90, 91, 92, 93],
    }


class AnalysisTests(unittest.TestCase):
    def test_distance_alignment_interpolates_on_shared_axis(self):
        lap = Lap(1, 90_000, False, samples=[sample(0, 0, 100), sample(100, 40_000, 200), sample(200, 90_000, 300)], saved_at="one")
        trace, quality = aligned_trace(lap, points=5)
        self.assertEqual(quality, "distance")
        self.assertEqual([row["distance_pct"] for row in trace], [0, 25, 50, 75, 100])
        self.assertEqual(trace[1]["speed_kph"], 150)

    def test_negative_delta_means_candidate_is_faster(self):
        baseline = Lap(1, 90_000, False, samples=[sample(0, 0), sample(200, 90_000)], saved_at="base", peak_speed_kph=200, minimum_speed_kph=200)
        candidate = Lap(2, 89_000, False, samples=[sample(0, 0), sample(200, 89_000)], saved_at="candidate", peak_speed_kph=200, minimum_speed_kph=200)
        comparison = compare_laps(baseline, candidate)
        self.assertEqual(comparison["summary"]["final_delta_s"], -1.0)
        self.assertEqual(comparison["trace"][-1]["delta_s"], -1.0)

    def test_verified_profile_allows_cross_session_race_comparison(self):
        samples = [
            {"lap_distance_m": 0, "current_lap_ms": 0, "speed_kph": 100, "throttle": 0, "brake": 0, "steering": 0, "gear": 2},
            {"lap_distance_m": 5000, "current_lap_ms": 90_000, "speed_kph": 200, "throttle": 100, "brake": 0, "steering": 0, "gear": 7},
        ]
        first = Lap(1, 90_000, False, samples=samples, saved_at="a", recording_session_id="race-a", mode="race", track_id=2, track_length_m=5000)
        second = Lap(1, 91_000, False, samples=samples, saved_at="b", recording_session_id="race-b", mode="race", track_id=2, track_length_m=5000)
        profile = {"id": "verified", "circuit_name": "Shanghai", "verification_status": "verified", "measured_game_length_m": 5000, "turns": []}
        self.assertEqual(compare_laps(first, second, profile)["summary"]["final_delta_s"], 1.0)

    def test_profile_turn_metrics_include_estimated_apex_exit_and_input_thresholds(self):
        baseline_samples = [
            {**sample(0, 0, 250), "brake": 0, "throttle": 100, "steering": 0},
            {**sample(100, 10_000, 150), "brake": 80, "throttle": 0, "steering": 20},
            {**sample(200, 20_000, 120), "brake": 0, "throttle": 30, "steering": 30},
            {**sample(300, 30_000, 190), "brake": 0, "throttle": 100, "steering": 0},
        ]
        candidate_samples = [dict(row, current_lap_ms=row["current_lap_ms"] + index * 100) for index, row in enumerate(baseline_samples)]
        baseline = Lap(1, 30_000, False, samples=baseline_samples, saved_at="turn-a")
        candidate = Lap(2, 30_300, False, samples=candidate_samples, saved_at="turn-b")
        profile = {
            "id": "test", "circuit_name": "Test", "verification_status": "verified",
            "measured_game_length_m": 300,
            "centreline": [
                {"distance_m": 300, "x": 3, "z": 4},
                {"distance_m": 0, "x": 1, "z": 2},
                {"distance_m": "bad", "x": 0, "z": 0},
            ],
            "turns": [{"number": 1, "name": "Test turn", "entry_m": 0, "estimated_apex_m": 200, "exit_m": 300, "direction": "right", "verification_status": "verified"}],
        }
        comparison = compare_laps(baseline, candidate, profile)
        turn = comparison["engineer_notes"]["windows"][0]
        self.assertEqual(turn["estimated_apex_m"], 200)
        self.assertIn("candidate_exit_speed_kph", turn["evidence"])
        self.assertTrue(any("estimated apex" in text for text in turn["statements"]))
        self.assertEqual(comparison["circuit_map"]["profile"]["id"], "test")
        self.assertEqual(comparison["circuit_map"]["centreline"][0]["distance_m"], 0)
        self.assertEqual(comparison["circuit_map"]["turns"][0]["number"], 1)


class SessionStoreTests(unittest.TestCase):
    def test_setup_is_exposed_in_live_state(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            setup = {"front_wing": 18, "rear_wing": 18, "on_throttle_diff": 25, "off_throttle_diff": 50}
            store.record_setup(setup)
            latest = store.snapshot()["latest"]
            self.assertEqual({key: latest["setup"][key] for key in setup}, setup)
            self.assertTrue(all(value == "decoded_udp" for value in latest["setup"]["_provenance"].values()))
            self.assertEqual(latest["front_wing"], 18)

    def test_old_capture_loads_and_uses_legacy_alignment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lap-20250101T000000Z-3.json"
            payload = {
                "number": 3, "time_ms": 91_000, "invalid": False,
                "samples": [{"t": 10.0, "speed_kph": 100}, {"t": 101.0, "speed_kph": 300}],
                "setup": None, "saved_at": "20250101T000000Z",
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = SessionStore(Path(directory))
            self.assertEqual(len(store.list_laps()), 1)
            trace, quality = aligned_trace(store.get_lap_object("lap-20250101T000000Z-3"), points=3)
            self.assertEqual(quality, "legacy-time")
            self.assertEqual(trace[1]["speed_kph"], 200)

    def test_new_sample_keeps_distance_and_lap_time(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_lap_state({"lap_number": 1, "last_lap_ms": 0, "current_lap_ms": 12_345, "lap_distance_m": 800.5, "invalid": False})
            store.record_telemetry({"speed_kph": 250}, 20.0)
            latest = store.snapshot()["latest"]
            self.assertEqual(latest["lap_distance_m"], 800.5)
            self.assertEqual(latest["current_lap_ms"], 12_345)

    def test_race_time_trial_and_game_invalid_laps_compare_with_warnings(self):
        samples = [sample(0, 0), sample(5891, 90_000)]
        race = Lap(1, 90_000, False, samples=samples, saved_at="race", recording_session_id="race-1", mode="race", track_id=7)
        trial = Lap(1, 89_000, True, samples=samples, saved_at="tt", recording_session_id="tt-1", mode="time_trial", track_id=7)
        comparison = compare_laps(race, trial)
        self.assertEqual(comparison["summary"]["final_delta_s"], -1.0)
        self.assertFalse(comparison["data_quality"]["like_for_like_mode"])
        self.assertFalse(comparison["data_quality"]["both_game_valid"])
        self.assertTrue(any("Race and Time Trial" in warning for warning in comparison["comparison_warnings"]))
        self.assertTrue(any("Candidate is game-invalid" in warning for warning in comparison["comparison_warnings"]))

    def test_different_tracks_and_partial_distance_capture_are_rejected(self):
        samples = [sample(0, 0), sample(5891, 90_000)]
        trial = Lap(1, 89_000, False, samples=samples, saved_at="tt", recording_session_id="tt-1", mode="time_trial", track_id=7, track_length_m=5891)
        other_track = Lap(1, 89_000, False, samples=samples, saved_at="other", recording_session_id="tt-2", mode="time_trial", track_id=8)
        with self.assertRaisesRegex(ComparisonError, "different tracks"):
            compare_laps(trial, other_track)
        partial = Lap(2, 45_000, True, samples=[sample(2800, 0), sample(5890, 45_000)], saved_at="partial", recording_session_id="tt-2", mode="time_trial", track_id=7, track_length_m=5891)
        with self.assertRaisesRegex(ComparisonError, "partial laps cannot be compared"):
            compare_laps(trial, partial)

    def test_legacy_alignment_suppresses_corner_notes(self):
        old_samples = [{**sample(None, 0), "lap_distance_m": None}, {**sample(None, 90_000), "lap_distance_m": None}]
        first = Lap(1, 90_000, False, samples=old_samples, saved_at="old-a")
        second = Lap(2, 89_500, False, samples=old_samples, saved_at="old-b")
        comparison = compare_laps(first, second)
        self.assertEqual(comparison["alignment"]["baseline"], "legacy-time")
        self.assertEqual(comparison["engineer_notes"]["windows"], [])
        self.assertIn("Legacy approximation", comparison["engineer_notes"]["calibration"])

    def test_final_delta_is_smoothed_to_official_time(self):
        base_samples = [sample(0, 500), sample(3000, 45_000), sample(5891, 88_000)]
        candidate_samples = [sample(0, 500), sample(3000, 45_000), sample(5891, 88_000)]
        baseline = Lap(1, 90_000, False, samples=base_samples, saved_at="smooth-a", recording_session_id="tt-a", mode="time_trial", track_id=7)
        candidate = Lap(1, 89_000, False, samples=candidate_samples, saved_at="smooth-b", recording_session_id="tt-b", mode="time_trial", track_id=7)
        trace = compare_laps(baseline, candidate)["trace"]
        self.assertAlmostEqual(trace[0]["delta_s"], 0.0, places=4)
        self.assertAlmostEqual(trace[-1]["delta_s"], -1.0, places=4)
        self.assertLess(abs(trace[-1]["delta_s"] - trace[-2]["delta_s"]), 0.01)


if __name__ == "__main__":
    unittest.main()
