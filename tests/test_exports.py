import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from io import BytesIO

from src.f1telemetry.reports import ExportError
from src.f1telemetry.receiver import SessionStore
from tests.test_sessions import UID, fill_lap, game, lap_state, telemetry


class ExportTests(unittest.TestCase):
    def _record_session(self, directory):
        store = SessionStore(Path(directory))
        store.record_game_session(game())
        store.record_setup({"front_wing": 19, "rear_wing": 17, "on_throttle_diff": 20, "off_throttle_diff": 45}, UID)
        store.record_lap_state(lap_state(1), UID)
        run = store.start_time_trial_run("18/18 baseline: report")
        fill_lap(store, UID, 1, 90_000)
        for index in range(30):
            current = int(index / 29 * 90_000)
            distance = index / 29 * 5880
            store.record_lap_state(lap_state(2, current, distance, invalid=True), UID)
            store.record_telemetry(telemetry(170 + index), current / 1000, UID)
        store.record_lap_state(lap_state(3, 0, 0, 92_000), UID)
        store.stop_recording()
        laps = store.session_laps[run["id"]]
        store.set_note(laps[0].id, "baseline note")
        return store, run["id"], laps

    def test_whole_markdown_includes_invalid_laps_and_notes(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            _name, mime, body = store.export_session(session_id, "session", [], "markdown")
            report = body.decode()
            self.assertEqual(mime, "text/markdown; charset=utf-8")
            self.assertIn("18/18 baseline: report", report)
            self.assertIn("baseline note", report)
            self.assertIn("INVALID", report)
            self.assertIn(f"| {laps[0].number} | valid", report)
            self.assertIn(f"| {laps[1].number} | INVALID", report)
            self.assertIn("Front camber: unknown", report)
            self.assertIn("decoded from UDP", report)

    def test_selected_zip_contains_only_selected_raw_lap(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            raw_path = Path(directory) / "sessions" / session_id / f"{laps[1].id}.json"
            original = raw_path.read_bytes()
            _name, mime, body = store.export_session(session_id, "selected", [laps[1].id], "zip")
            self.assertEqual(mime, "application/zip")
            with ZipFile(BytesIO(body)) as archive:
                lap_files = [name for name in archive.namelist() if name.startswith("laps/")]
                self.assertEqual(len(lap_files), 1)
                self.assertEqual(archive.read(lap_files[0]), original)
                context = json.loads(archive.read("session.json"))
                self.assertEqual(context["exported_lap_ids"], [laps[1].id])

    def test_empty_selection_is_rejected(self):
        with TemporaryDirectory() as directory:
            store, session_id, _laps = self._record_session(directory)
            with self.assertRaisesRegex(ExportError, "Select at least one"):
                store.export_session(session_id, "selected", [], "markdown")

    def test_export_does_not_modify_original_files(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            directory_path = Path(directory) / "sessions" / session_id
            before = {path.name: path.read_bytes() for path in directory_path.glob("*.json")}
            store.export_session(session_id, "session", [], "zip")
            after = {path.name: path.read_bytes() for path in directory_path.glob("*.json")}
            self.assertEqual(before, after)
            self.assertEqual(len(laps), 2)


if __name__ == "__main__":
    unittest.main()
