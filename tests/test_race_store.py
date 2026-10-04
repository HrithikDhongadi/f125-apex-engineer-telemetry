from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from io import BytesIO

from src.f1telemetry.circuit_profiles import CircuitProfileError, CircuitProfileStore, profile_identity
from src.f1telemetry.race_store import RaceSessionStore
from src.f1telemetry.receiver import SessionStore


def packet(frame: int, packet_id: int = 2):
    return SimpleNamespace(
        packet_format=2025, game_year=25, game_major_version=1, game_minor_version=0,
        session_uid=7, session_time=float(frame), frame_identifier=frame,
        overall_frame_identifier=frame, packet_id=packet_id, packet_version=1,
        player_car_index=0, secondary_player_car_index=255,
    )


class RaceStoreTests(unittest.TestCase):
    def test_incremental_packets_survive_unclosed_writer_and_track_loss(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            store = RaceSessionStore(path)
            store.append_packet(packet(1), {"player": {"lap_distance_m": 10}}, 10)
            store.append_packet(packet(3), {"player": {"lap_distance_m": 30}}, 30)
            store.append_event(3, "lights_out", {}, "inferred", 1)
            store.append_event(3, "position_change", {"from": 10, "to": 9}, "inferred", 1)

            # A second process can read committed rows before graceful close.
            reader = sqlite3.connect(path)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 2)
            header_payload = reader.execute("SELECT header_json FROM packets ORDER BY sequence LIMIT 1").fetchone()[0]
            self.assertIn('"packet_format":2025', header_payload)
            reader.close()
            self.assertEqual(store.summary()["missing_frame_estimate"], 1)
            self.assertEqual([item["type"] for item in store.timeline()], ["lights_out", "position_change"])
            selected = store.export_packets(2.5, 3.5).decode("utf-8").splitlines()
            self.assertEqual(len(selected), 1)
            self.assertEqual(json.loads(selected[0])["overall_frame_identifier"], 3)
            store.close()
            self.assertEqual(len(RaceSessionStore.read_packet_export(path, 0, 2).splitlines()), 1)

    def test_race_persists_incomplete_lap_without_creating_lap_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session({"session_uid": 7, "session_type": 15, "mode": "race", "track_id": 7, "track_length_m": 5890})
            session_id = store.active_recording_id
            store.record_full_packet(packet(1), {"player": {"lap_number": 1, "lap_distance_m": 500, "pit_status": 0, "position": 12}})
            store.stop_recording()
            self.assertEqual(store.session_laps[session_id], [])
            database = root / "sessions" / session_id / "race-data.sqlite3"
            connection = sqlite3.connect(database)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 1)
            connection.close()
            _, _, archive_bytes = store.export_session(session_id, "session", [], "zip")
            with ZipFile(BytesIO(archive_bytes)) as archive:
                self.assertIn("race/race-data.sqlite3", archive.namelist())
                self.assertIn("race/timeline.json", archive.namelist())
                snapshot = root / "snapshot-check.sqlite3"
                snapshot.write_bytes(archive.read("race/race-data.sqlite3"))
                exported = sqlite3.connect(snapshot)
                self.assertEqual(exported.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 1)
                exported.close()

    def test_profiles_keep_reverse_identity_and_validate_turn_order(self):
        with TemporaryDirectory() as directory:
            profiles = CircuitProfileStore(Path(directory))
            self.assertNotEqual(profile_identity(2025, 7), profile_identity(2025, 39))
            saved = profiles.save({
                "packet_format": 2025, "track_id": 39, "layout": "reverse",
                "circuit_name": "Silverstone (Reverse)", "measured_game_length_m": 5890,
                "turns": [{"number": 1, "name": "Test", "entry_m": 100, "estimated_apex_m": 150, "exit_m": 200, "direction": "left"}],
                "provenance": {"source": "test fixture"},
            })
            self.assertEqual(saved["track_id"], 39)
            with self.assertRaises(CircuitProfileError):
                profiles.save({"packet_format": 2025, "track_id": 2, "measured_game_length_m": 5000, "turns": [{"entry_m": 200, "estimated_apex_m": 100, "exit_m": 300}]})


if __name__ == "__main__":
    unittest.main()
