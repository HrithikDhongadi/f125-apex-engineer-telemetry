from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from io import BytesIO

from src.f1telemetry.circuit_profiles import CircuitProfileError, CircuitProfileStore, profile_audit, profile_identity
from src.f1telemetry.race_store import RaceSessionStore
from src.f1telemetry.receiver import SessionStore


def packet(frame: int, packet_id: int = 2, session_uid: int = 7):
    return SimpleNamespace(
        packet_format=2025, game_year=25, game_major_version=1, game_minor_version=0,
        session_uid=session_uid, session_time=float(frame), frame_identifier=frame,
        overall_frame_identifier=frame, packet_id=packet_id, packet_version=1,
        player_car_index=0, secondary_player_car_index=255,
    )


def framed_packet(frame: int, overall: int, session_time: float, packet_id: int, player: int = 19):
    value = packet(frame, packet_id)
    value.frame_identifier = frame
    value.overall_frame_identifier = overall
    value.session_time = session_time
    value.player_car_index = player
    return value


def lap_payload(player_position: int, opponent_position: int, distance: float = 1000.0):
    cars = [{"car_index": index, "position": index + 1, "lap_distance_m": distance, "lap_number": 1} for index in range(22)]
    cars[19]["position"] = player_position
    cars[8]["position"] = opponent_position
    return {"cars": cars, "player": cars[19]}


def udp_event(store, frame: int, overall: int, session_time: float, payload: dict):
    header = framed_packet(frame, overall, session_time, 3)
    store.append_packet(header, payload)
    names = {"OVTK": "overtake", "COLL": "collision", "PENA": "penalty", "FLBK": "flashback"}
    store.append_event(session_time, names[payload["code"]], payload, "udp_event", 1)


class RaceStoreTests(unittest.TestCase):
    def test_unsigned_64_bit_session_uid_is_stored_losslessly(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            store = RaceSessionStore(path)
            uid = 16_392_067_743_388_728_681
            store.append_packet(packet(1, session_uid=uid), {"player": {"lap_number": 1}})

            connection = sqlite3.connect(path)
            stored_uid, header_json = connection.execute(
                "SELECT session_uid,header_json FROM packets"
            ).fetchone()
            connection.close()
            self.assertEqual(stored_uid, uid - (1 << 64))
            self.assertEqual(json.loads(header_json)["session_uid"], uid)
            exported = json.loads(store.export_packets(0, 2).decode("utf-8"))
            self.assertEqual(exported["header"]["session_uid"], uid)
            store.close()

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

    def test_continuity_excludes_low_rate_packets_and_does_not_sum_streams(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            store = RaceSessionStore(path)
            for packet_id in (0, 2, 6, 7, 13):
                store.append_packet(packet(1, packet_id), {})
                store.append_packet(packet(3, packet_id), {})
            # Car Damage is normally lower-rate and intentionally skips frames.
            store.append_packet(packet(1, 10), {})
            store.append_packet(packet(60, 10), {})
            summary = store.summary()
            self.assertEqual(summary["missing_frame_estimate"], 1)
            self.assertEqual(summary["missing_frame_percent"], 33.333)
            self.assertEqual(len(summary["high_rate_streams"]), 5)
            self.assertEqual(summary["capture_loss_frame_estimate"], 0)
            self.assertEqual(summary["sequence_discontinuity_frame_estimate"], 1)
            store.close()

    def test_flashback_reconstruction_keeps_raw_branch_and_counts_only_replay(self):
        with TemporaryDirectory() as directory:
            store = RaceSessionStore(Path(directory) / "race.sqlite3")
            store.append_packet(framed_packet(5, 5, 5.0, 2), lap_payload(2, 1), 900)
            udp_event(store, 10, 10, 10.0, {"code": "OVTK", "overtaking_vehicle_index": 19, "overtaken_vehicle_index": 8})
            store.append_packet(framed_packet(11, 11, 10.1, 2), lap_payload(1, 2), 1000)
            udp_event(store, 12, 12, 10.2, {"code": "COLL", "vehicle_1_index": 19, "vehicle_2_index": 8})
            udp_event(store, 20, 20, 20.0, {"code": "FLBK", "flashback_frame_identifier": 5, "flashback_session_time": 5.0})
            store.append_packet(framed_packet(5, 21, 5.1, 2), lap_payload(2, 1), 900)
            udp_event(store, 6, 22, 6.0, {"code": "OVTK", "overtaking_vehicle_index": 19, "overtaken_vehicle_index": 8})
            store.append_packet(framed_packet(7, 23, 6.1, 2), lap_payload(1, 2), 1000)

            events = store.timeline()
            overtakes = [event for event in events if event["type"] == "overtake"]
            self.assertEqual([event["event_validity"] for event in overtakes], ["superseded", "accepted"])
            self.assertEqual(overtakes[0]["superseded_by"]["flashback_reference"]["frame_identifier"], 5)
            self.assertEqual(overtakes[1]["timeline_branch_id"], "branch-1")
            self.assertEqual(overtakes[1]["verification_status"], "confirmed")
            self.assertEqual(overtakes[1]["player_position_before"], 2)
            self.assertEqual(overtakes[1]["player_position_after"], 1)
            collision = next(event for event in events if event["type"] == "collision")
            self.assertEqual(collision["event_validity"], "superseded")
            store.close()

    def test_warning_is_not_an_actual_penalty_and_collision_messages_are_grouped(self):
        with TemporaryDirectory() as directory:
            store = RaceSessionStore(Path(directory) / "race.sqlite3")
            store.append_packet(framed_packet(1, 1, 1.0, 2), lap_payload(4, 3), 500)
            udp_event(store, 2, 2, 2.0, {"code": "PENA", "penalty_type": 5, "infringement_type": 4, "vehicle_index": 19, "other_vehicle_index": 8, "time_s": 255, "lap_number": 1, "places_gained": 0})
            udp_event(store, 3, 3, 3.0, {"code": "COLL", "vehicle_1_index": 19, "vehicle_2_index": 8})
            udp_event(store, 4, 4, 3.4, {"code": "COLL", "vehicle_1_index": 8, "vehicle_2_index": 19})
            udp_event(store, 5, 5, 5.0, {"code": "COLL", "vehicle_1_index": 19, "vehicle_2_index": 8})
            events = store.timeline()
            warning = next(event for event in events if event["type"] == "penalty")
            self.assertEqual(warning["penalty_classification"], "warning")
            self.assertEqual(warning["penalty_label"], "warning")
            self.assertEqual(warning["infringement_label"], "small collision")
            collisions = [event for event in events if event["type"] == "collision"]
            self.assertEqual(len({event["collision_incident_id"] for event in collisions}), 2)
            self.assertEqual(collisions[1]["collision_incident_role"], "repeated_message")
            store.close()

    def test_setup_audit_selects_stable_player_setup_without_rewriting_history(self):
        with TemporaryDirectory() as directory:
            store = RaceSessionStore(Path(directory) / "race.sqlite3")
            old = {"player": {"front_wing": 0, "rear_wing": 0, "on_throttle_diff": 90, "fuel_load_kg": 10.0}}
            active = {"player": {"front_wing": 0, "rear_wing": 0, "on_throttle_diff": 45, "fuel_load_kg": 17.0}}
            for frame in (0, 30, 60):
                store.append_packet(framed_packet(frame, frame, 0.0, 5), old)
            for frame in (90, 120, 150, 180):
                store.append_packet(framed_packet(frame, frame, 0.0, 5), active)
            audit = store.summary()["setup_snapshot_audit"]
            self.assertEqual(audit["status"], "confirmed_active")
            self.assertEqual(audit["selected_frame_identifier"], 90)
            self.assertEqual(audit["selected_setup"]["fuel_load_kg"], 17.0)
            self.assertEqual(audit["history"][0]["status"], "superseded")
            self.assertEqual(audit["wing_verification"], "unverified_as_transmitted_zero")
            store.close()

    @unittest.skipUnless(
        Path("data/sessions/session-20261008T090243-5dd3a5b6/race-data.sqlite3").exists(),
        "recorded Spa regression dataset is not installed",
    )
    def test_recorded_spa_flashback_reconciliation(self):
        path = Path("data/sessions/session-20261008T090243-5dd3a5b6/race-data.sqlite3")
        events = RaceSessionStore.read_timeline(path)
        player = next(event["player_car_index"] for event in events if event["player_car_index"] is not None)
        accepted = [event for event in events if event["event_validity"] == "accepted"]
        self.assertEqual(sum(event["type"] == "overtake" and event.get("verification_status") == "confirmed" and event["details"].get("overtaking_vehicle_index") == player for event in accepted), 9)
        self.assertEqual(sum(event["type"] == "overtake" and event.get("verification_status") == "confirmed" and event["details"].get("overtaken_vehicle_index") == player for event in accepted), 4)
        self.assertEqual(sum(event["type"] == "collision" and event["player_relevant"] is True for event in accepted), 10)
        warning = next(event for event in accepted if event["type"] == "penalty" and event["player_relevant"] is True)
        self.assertEqual((warning["penalty_classification"], warning["infringement_label"]), ("warning", "small collision"))

    def test_timeline_marks_other_car_targeted_events_without_deleting_them(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            store = RaceSessionStore(path)
            header = packet(1)
            header.player_car_index = 19
            store.append_packet(header, {})
            store.append_event(1, "overtake", {"overtaking_vehicle_index": 10, "overtaken_vehicle_index": 17}, "udp_event", 1)
            store.append_event(2, "collision", {"vehicle_1_index": 19, "vehicle_2_index": 8}, "udp_event", 1)
            events = store.timeline()
            self.assertFalse(events[0]["player_relevant"])
            self.assertEqual(events[0]["scope"], "other_car")
            self.assertTrue(events[1]["player_relevant"])
            self.assertEqual(len(events), 2)

    def test_event_attribution_uses_source_packet_player_index(self):
        with TemporaryDirectory() as directory:
            store = RaceSessionStore(Path(directory) / "race.sqlite3")
            store.append_packet(framed_packet(1, 1, 1.0, 2, player=19), lap_payload(4, 3))
            payload = {"code": "COLL", "vehicle_1_index": 5, "vehicle_2_index": 8}
            header = framed_packet(2, 2, 2.0, 3, player=5)
            store.append_packet(header, payload)
            store.append_event(2.0, "collision", payload, "udp_event", 1)
            event = store.timeline()[0]
            self.assertEqual(event["player_car_index"], 5)
            self.assertTrue(event["player_relevant"])
            store.close()

    def test_race_persists_incomplete_lap_without_creating_lap_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root)
            store.record_game_session({"session_uid": 7, "session_type": 15, "mode": "race", "track_id": 7, "track_length_m": 5890})
            session_id = store.active_recording_id
            store.record_full_packet(packet(1), {"player": {"lap_number": 1, "lap_distance_m": 500, "pit_status": 0, "position": 12}})
            store.stop_recording()
            self.assertEqual(store.session_laps[session_id], [])
            library_entry = next(item for item in store.list_sessions() if item["id"] == session_id)
            self.assertTrue(library_entry["continuous_store"]["details_deferred"])
            self.assertNotIn("packet_count", library_entry["continuous_store"])
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

    def test_profile_audit_does_not_trust_global_verified_label(self):
        audit = profile_audit({
            "verification_status": "verified",
            "turns": [
                {"number": "S1", "name": "Suggested turn", "entry_m": 100, "estimated_apex_m": 150, "exit_m": 220, "verification_status": "unverified"},
                {"number": "S2", "name": "Named", "entry_m": 210, "estimated_apex_m": 240, "exit_m": 280, "verification_status": "verified"},
            ],
        })
        self.assertFalse(audit["ready_for_corner_analysis"])
        self.assertEqual(audit["overlaps"][0]["overlap_m"], 10.0)
        self.assertTrue(any("individual turns" in warning for warning in audit["warnings"]))

    def test_saved_profile_deletion_moves_file_to_recoverable_trash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profiles = CircuitProfileStore(root / "circuit_profiles")
            saved = profiles.save({
                "id": "spa-editable-v1", "packet_format": 2025, "track_id": 10,
                "layout": "normal", "measured_game_length_m": 7004,
                "turns": [{"number": 1, "entry_m": 50, "estimated_apex_m": 100, "exit_m": 150}],
            })
            result = profiles.delete(saved["id"])
            self.assertTrue(result["recoverable"])
            self.assertIsNone(profiles.get(saved["id"]))
            self.assertTrue((root / result["moved_to"]).exists())
            with self.assertRaisesRegex(CircuitProfileError, "built-in"):
                profiles.delete("f1-2025-track-7-normal-v1")


if __name__ == "__main__":
    unittest.main()
