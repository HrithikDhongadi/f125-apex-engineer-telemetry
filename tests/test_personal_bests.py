from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.f1telemetry.analysis import compare_laps
from src.f1telemetry.personal_bests import PersonalBestRegistry, pb_key
from src.f1telemetry.receiver import Lap, SessionStore


def complete_lap(time_ms=90_000, invalid=False, track_id=7, mode="time_trial", session_id="run-1"):
    samples = []
    for index in range(31):
        progress = index / 30
        samples.append({
            "t": progress * time_ms / 1000, "lap_distance_m": progress * 5891,
            "current_lap_ms": progress * (time_ms - 300), "speed_kph": 150 + index,
            "throttle": 100, "brake": 0, "steering": 0, "gear": 7,
        })
    return Lap(
        1, time_ms, invalid, samples=samples, setup={"front_wing": 18, "rear_wing": 18},
        saved_at=f"lap-{time_ms}", sector1_ms=28_000, sector2_ms=39_000,
        recording_session_id=session_id, game_session_uid=12, mode=mode,
        track_id=track_id, track_length_m=5891,
    )


class PersonalBestTests(unittest.TestCase):
    def test_first_faster_and_slower_valid_laps(self):
        with TemporaryDirectory() as directory:
            registry = PersonalBestRegistry(Path(directory))
            first = registry.consider(complete_lap(90_000), "baseline")
            self.assertIsNotNone(first)
            self.assertEqual(first["time_ms"], 90_000)
            self.assertIsNone(registry.consider(complete_lap(91_000), "slower"))
            faster = registry.consider(complete_lap(89_000, session_id="run-2"), "faster")
            self.assertEqual(faster["time_ms"], 89_000)
            self.assertEqual(registry.get(pb_key(7, "time_trial"))["time_ms"], 89_000)

    def test_invalid_partial_and_unknown_track_do_not_replace(self):
        with TemporaryDirectory() as directory:
            registry = PersonalBestRegistry(Path(directory))
            registry.consider(complete_lap(90_000), "baseline")
            self.assertIsNone(registry.consider(complete_lap(80_000, invalid=True), "invalid"))
            partial = complete_lap(80_000)
            partial.samples = partial.samples[10:20]
            self.assertIsNone(registry.consider(partial, "partial"))
            self.assertIsNone(registry.consider(complete_lap(79_000, track_id=-1), "unknown"))
            self.assertIsNone(registry.consider(complete_lap(78_000, track_id=99), "unrecognised positive ID"))
            self.assertEqual(registry.get(pb_key(7, "time_trial"))["time_ms"], 90_000)

    def test_registry_persists_full_lap_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            registry = PersonalBestRegistry(path)
            lap = complete_lap()
            registry.consider(lap, "baseline", "clean lap")
            reloaded = PersonalBestRegistry(path)
            entry = reloaded.get(pb_key(7, "time_trial"))
            self.assertEqual(entry["note"], "clean lap")
            self.assertEqual(len(entry["lap"]["samples"]), len(lap.samples))
            self.assertFalse(list(path.glob("*.tmp")))

    def test_official_history_pb_retains_separate_captured_comparison_trace(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            registry = PersonalBestRegistry(path)
            captured = complete_lap(105_317, track_id=10, session_id="captured-run")
            registry.consider(captured, "captured session")

            official = registry.consider_official_history(
                track_id=10, track_length_m=7007, mode="time_trial",
                time_ms=104_954, sector1_ms=31_265, sector2_ms=44_967,
                sector3_ms=28_722, lap_number=9, valid_flags=15,
                session_id="history-run", session_name="Spa TT",
                game_session_uid=123,
            )

            self.assertIsNotNone(official)
            self.assertEqual(official["time_ms"], 104_954)
            self.assertEqual(official["sector3_ms"], 28_722)
            self.assertTrue(official["official_time_only"])
            self.assertEqual(official["comparison_lap_time_ms"], 105_317)
            self.assertEqual(official["lap"]["time_ms"], 105_317)
            self.assertIsNone(official["setup"])

            # A quicker complete trace improves comparison fidelity without
            # replacing the still-faster official PB time.
            registry.consider(complete_lap(105_000, track_id=10, session_id="better-trace"), "better trace")
            current = PersonalBestRegistry(path).get(pb_key(10, "time_trial"))
            self.assertEqual(current["time_ms"], 104_954)
            self.assertEqual(current["comparison_lap_time_ms"], 105_000)
            self.assertEqual(current["lap"]["time_ms"], 105_000)

    def test_invalid_session_history_does_not_replace_pb(self):
        with TemporaryDirectory() as directory:
            registry = PersonalBestRegistry(Path(directory))
            registry.consider(complete_lap(105_317, track_id=10), "captured")
            result = registry.consider_official_history(
                track_id=10, track_length_m=7007, mode="time_trial",
                time_ms=104_000, sector1_ms=31_000, sector2_ms=44_000,
                sector3_ms=29_000, lap_number=9, valid_flags=7,
                session_id="history-run", session_name="Spa TT",
                game_session_uid=123,
            )
            self.assertIsNone(result)
            self.assertEqual(registry.get(pb_key(10, "time_trial"))["time_ms"], 105_317)

    def test_historical_rebuild_and_compatible_pb_comparison(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            old = complete_lap(89_500, session_id="historic-run")
            store.laps.append(old)
            result = store.rebuild_personal_bests()
            self.assertEqual(result["personal_best_count"], 1)
            baseline = store.get_lap_object(f"pb:{pb_key(7, 'time_trial')}")
            candidate = complete_lap(89_000, session_id="future-run")
            comparison = compare_laps(baseline, candidate)
            self.assertEqual(comparison["summary"]["final_delta_s"], -0.5)


if __name__ == "__main__":
    unittest.main()
