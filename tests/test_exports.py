import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from threading import Event, Thread
from zipfile import ZipFile
from io import BytesIO

from src.f1telemetry.application.offline_reports import OfflineReportService
from src.f1telemetry.reports import ExportError, build_markdown
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
        store.record_full_packet(SimpleNamespace(
            packet_format=2025, game_year=25, game_major_version=1, game_minor_version=0,
            packet_version=1, packet_id=6, session_uid=UID, session_time=45.0,
            frame_identifier=900, overall_frame_identifier=901,
            player_car_index=0, secondary_player_car_index=255,
        ), {"player": {"speed_kph": 250}})
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
                revision = json.loads(archive.read("export-revision.json"))
                self.assertEqual(revision["completed_lap_ids"], [laps[1].id])
                self.assertIsInstance(revision["revision_id"], str)
                self.assertEqual(len(revision["revision_id"]), 64)
                self.assertGreaterEqual(revision["packet_watermark"], 1)
                profile = json.loads(archive.read("circuit-profile.json"))
                self.assertEqual(profile["track_id"], 7)
                self.assertEqual(profile["verification_status"], "approximate")
                packet_rows = archive.read("race/selected-packets.jsonl").splitlines()
                self.assertEqual(len(packet_rows), 1)
                self.assertEqual(json.loads(packet_rows[0])["packet_id"], 6)

    def test_report_contains_race_context_and_timeline_provenance(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            laps[0].race_context = {"start": {
                "lap": {"position": 4, "delta_to_car_in_front_ms": 750, "delta_to_race_leader_ms": 2200, "pit_status": 0},
                "car_status": {"fuel_kg": 42.5, "actual_tyre_compound": 18, "tyre_age_laps": 3, "ers_store_j": 3_100_000},
                "damage": {"tyre_wear_pct": [8, 9, 10, 11]},
                "session": {"safety_car_status": 0, "weather": 1},
            }, "finish": {
                "lap": {"position": 3, "delta_to_car_in_front_ms": 600, "delta_to_race_leader_ms": 1900, "pit_status": 0},
                "car_status": {"fuel_kg": 40.2, "actual_tyre_compound": 18, "tyre_age_laps": 3, "ers_store_j": 2_900_000},
                "damage": {"tyre_wear_pct": [10, 11, 12, 13], "tyre_damage_pct": [0, 0, 0, 0], "tyre_blisters_pct": [0, 0, 0, 0]},
                "session": {"safety_car_status": 0, "weather": 1},
            }}
            for sample_row in laps[0].samples:
                sample_row["tyre_surface_c"] = [88, 89, 90, 91]
                sample_row["tyre_pressures_psi"] = [21.0, 21.1, 24.0, 24.1]
            _name, _mime, body = store.export_session(session_id, "session", [], "markdown")
            report = body.decode()
            self.assertIn("fuel 42.5 kg", report)
            self.assertIn("gap ahead 750 ms", report)
            self.assertIn("recording_started · recorder", report)
            self.assertIn("Continuous packets: 1", report)
            self.assertIn("Worst individual high-rate stream", report)
            self.assertIn("High-rate continuity", report)
            self.assertIn("Packets dropped by storage limit: 0", report)
            self.assertIn("Profile verification: approximate", report)
            self.assertIn("estimated apex", report.lower())
            self.assertIn("Detailed lap engineering sheets", report)
            self.assertIn("Tyres, degradation and temperatures", report)
            self.assertIn("Measured inner temperature maximum", report)
            self.assertIn("Measured running pressure average", report)
            self.assertIn("not configured setup pressure", report)
            self.assertIn("rear left 2.00 percentage points", report)
            self.assertIn("rear left 21.00 PSI", report)
            self.assertIn("calculated net used 2.30 kg", report)
            self.assertIn("Configured starting fuel", report)

    def test_report_hides_repeated_rejected_crossing_decisions(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            session = store.sessions[session_id]
            session.mode = "race"
            for lap in laps:
                lap.mode = "race"
            timeline = [
                {
                    "session_time": index / 60, "lap_number": laps[0].number,
                    "type": "rejected_crossing", "source": "recorder",
                    "event_validity": "accepted", "timeline_branch_id": "branch-0",
                }
                for index in range(120)
            ]
            timeline.append({
                "session_time": 0.0, "lap_number": laps[0].number,
                "type": "recording_started", "source": "recorder",
                "event_validity": "accepted", "timeline_branch_id": "branch-0",
            })

            report = build_markdown(session, laps, {}, "Silverstone", timeline, report_type="race")
            self.assertNotIn("rejected crossing", report)
            self.assertNotIn("rejected_crossing", report)
            self.assertIn("recording_started · recorder", report)

    def test_distinct_time_trial_race_and_lap_analysis_reports(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            tt_name, _, tt_body = store.export_session(session_id, "session", [], "markdown", "time_trial")
            lap_name, _, lap_body = store.export_session(session_id, "selected", [laps[0].id], "markdown", "lap_analysis")
            self.assertIn("Time Trial Report", tt_body.decode())
            self.assertIn("Valid-lap consistency", tt_body.decode())
            self.assertIn("time-trial", tt_name)
            self.assertIn("Lap Analysis Report", lap_body.decode())
            self.assertIn("lap-analysis", lap_name)
            with self.assertRaisesRegex(ExportError, "race session"):
                store.export_session(session_id, "session", [], "markdown", "race")

            store.sessions[session_id].mode = "race"
            for lap in laps:
                lap.mode = "race"
            race_name, _, race_body = store.export_session(session_id, "session", [], "markdown", "race")
            self.assertIn("Race Report", race_body.decode())
            self.assertIn("Stint analysis", race_body.decode())
            self.assertIn("race", race_name)

    def test_direct_comparison_report_contains_turn_and_lap_detail(self):
        with TemporaryDirectory() as directory:
            store, _session_id, laps = self._record_session(directory)
            name, mime, body = store.export_lap_analysis(laps[0].id, laps[0].id)
            report = body.decode()
            self.assertEqual(mime, "text/markdown; charset=utf-8")
            self.assertIn("Lap Analysis Report", report)
            self.assertIn("Circuit-profile turn analysis", report)
            self.assertIn("Baseline engineering sheet", report)
            self.assertIn("analysis", name)

    def test_direct_comparison_report_warns_for_game_invalid_lap(self):
        with TemporaryDirectory() as directory:
            store, _session_id, laps = self._record_session(directory)
            _name, _mime, body = store.export_lap_analysis(laps[0].id, laps[1].id)
            report = body.decode()
            self.assertIn("Comparison warnings and data quality", report)
            self.assertIn("Candidate is game-invalid", report)
            self.assertIn("diagnosis only", report)

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

    def test_zip_can_be_generated_as_temporary_streaming_artifact(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            name, mime, path = store.export_session(
                session_id, "selected", [laps[0].id], "zip", file_output=True,
            )
            self.assertTrue(name.endswith(".zip"))
            self.assertEqual(mime, "application/zip")
            self.assertIsInstance(path, Path)
            try:
                with ZipFile(path) as archive:
                    self.assertIn("export-revision.json", archive.namelist())
            finally:
                path.unlink(missing_ok=True)

    def test_recording_continues_while_large_zip_is_rendered(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            store.record_game_session(game())
            store.record_setup({"front_wing": 19, "rear_wing": 17}, UID)
            store.record_lap_state(lap_state(1), UID)
            run = store.start_time_trial_run("active export")
            fill_lap(store, UID, 1, 90_000)
            def packet(frame):
                return SimpleNamespace(
                    packet_format=2025, game_year=25, game_major_version=1, game_minor_version=0,
                    packet_version=1, packet_id=6, session_uid=UID, session_time=float(frame),
                    frame_identifier=frame, overall_frame_identifier=frame,
                    player_car_index=0, secondary_player_car_index=255,
                )

            store.record_full_packet(packet(900), {"player": {"speed_kph": 298}})
            entered, release = Event(), Event()
            result = []

            from src.f1telemetry.application import exports as export_module
            real_build_zip = export_module.build_zip_file

            def slow_build_zip(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return real_build_zip(*args, **kwargs)

            def run_export():
                result.append(store.export_session(run["id"], "session", [], "zip"))

            with patch("src.f1telemetry.application.exports.build_zip_file", side_effect=slow_build_zip):
                worker = Thread(target=run_export)
                worker.start()
                self.assertTrue(entered.wait(5))
                before = len(store.active_samples)
                store.record_telemetry(telemetry(299), 91.0, UID)
                store.record_full_packet(packet(901), {"player": {"speed_kph": 299}})
                self.assertEqual(len(store.active_samples), before + 1)
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0][1], "application/zip")
            with ZipFile(BytesIO(result[0][2])) as archive:
                revision = json.loads(archive.read("export-revision.json"))
                snapshot_path = Path(directory) / "captured-export.sqlite3"
                snapshot_path.write_bytes(archive.read("race/race-data.sqlite3"))
                connection = sqlite3.connect(snapshot_path)
                exported_packets = connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
                connection.close()
            self.assertEqual(revision["packet_watermark"], 1)
            self.assertEqual(exported_packets, 1)
            self.assertEqual(store.race_store.append_count, 2)

    def test_captured_export_survives_setup_amendment_and_session_deletion(self):
        with TemporaryDirectory() as directory:
            store, session_id, laps = self._record_session(directory)
            original = (Path(directory) / "sessions" / session_id / f"{laps[0].id}.json").read_bytes()
            entered, release = Event(), Event()
            result = []

            from src.f1telemetry.application import exports as export_module
            real_build_markdown = export_module.build_markdown

            def slow_report(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return real_build_markdown(*args, **kwargs)

            def run_export():
                result.append(store.export_session(session_id, "session", [], "zip"))

            with patch("src.f1telemetry.application.exports.build_markdown", side_effect=slow_report):
                worker = Thread(target=run_export)
                worker.start()
                self.assertTrue(entered.wait(5))
                store.amend_lap_setup(laps[0].id, {"rear_wing": 16})
                store.delete_session(session_id, confirmed=True)
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            with ZipFile(BytesIO(result[0][2])) as archive:
                exported = archive.read(f"laps/{laps[0].id}.json")
                self.assertEqual(exported, original)

    def test_offline_report_does_not_interrupt_open_session_or_purge_trash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _store, session_id, _laps = self._record_session(directory)
            manifest_path = root / "sessions" / session_id / "session.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "recording"
            manifest["ended_at"] = None
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            original_manifest = manifest_path.read_bytes()
            lap_paths = sorted(manifest_path.parent.glob("lap-*.json"))
            original_laps = {path.name: path.read_bytes() for path in lap_paths}

            trash = root / "trash" / "sessions" / "expired-but-preserved"
            trash.mkdir(parents=True)
            (trash / "deletion.json").write_text(json.dumps({
                "deleted_at": "2020-01-01T00:00:00Z",
                "session": {"id": "deleted-session", "name": "deleted"},
            }), encoding="utf-8")
            original_trash = (trash / "deletion.json").read_bytes()

            name, mime, body = OfflineReportService(root).export_markdown(session_id)

            self.assertTrue(name.endswith(".md"))
            self.assertEqual(mime, "text/markdown; charset=utf-8")
            self.assertIn("18/18 baseline: report", body.decode())
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(
                {path.name: path.read_bytes() for path in lap_paths},
                original_laps,
            )
            self.assertEqual((trash / "deletion.json").read_bytes(), original_trash)
            self.assertTrue(trash.is_dir())


if __name__ == "__main__":
    unittest.main()
