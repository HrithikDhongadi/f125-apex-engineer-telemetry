from pathlib import Path
import unittest

from src.f1telemetry.circuits import track_name


ROOT = Path(__file__).resolve().parents[1]


class DashboardContractTests(unittest.TestCase):
    def test_independent_session_and_lap_selectors_exist_for_both_sides(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for element_id in ("baseline-session", "baseline", "candidate-session", "candidate"):
            self.assertIn(f'id="{element_id}"', html)

    def test_official_and_reverse_track_ids_remain_distinct(self):
        self.assertEqual(track_name(7), "Silverstone")
        self.assertEqual(track_name(39), "Silverstone (Reverse)")
        self.assertEqual(track_name(99), "Unknown track (ID 99)")

    def test_circuit_profile_editor_exposes_trace_alternates_and_verification(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for element_id in (
            "profile-select", "profile-alternate", "profile-verification",
            "profile-source", "profile-lap", "profile-load-trace", "circuit-map",
            "profile-add-suggestions", "race-timeline",
        ):
            self.assertIn(f'id="{element_id}"', html)


if __name__ == "__main__":
    unittest.main()
