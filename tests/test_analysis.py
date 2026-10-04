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

    def test_incompatible_modes_and_tracks_are_rejected(self):
        samples = [sample(0, 0), sample(5891, 90_000)]
        race = Lap(1, 90_000, False, samples=samples, saved_at="race", recording_session_id="race-1", mode="race", track_id=7)
        trial = Lap(1, 89_000, False, samples=samples, saved_at="tt", recording_session_id="tt-1", mode="time_trial", track_id=7)
        with self.assertRaisesRegex(ComparisonError, "Race and Time Trial"):
            compare_laps(race, trial)
        other_track = Lap(1, 89_000, False, samples=samples, saved_at="other", recording_session_id="tt-2", mode="time_trial", track_id=8)
        with self.assertRaisesRegex(ComparisonError, "different tracks"):
            compare_laps(trial, other_track)

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
