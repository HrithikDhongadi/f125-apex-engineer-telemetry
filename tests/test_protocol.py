import struct
import unittest

from src.f1telemetry.protocol import (
    HEADER_SIZE,
    decode_header,
    decode_player_car_telemetry,
    decode_player_setup,
    decode_session,
)


class ProtocolTests(unittest.TestCase):
    def test_header_and_player_telemetry_decode(self):
        header = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 6, 42, 10.5, 17, 17, 0, 255)
        car = bytearray(60)
        struct.pack_into("<Hfff", car, 0, 301, 0.85, -0.15, 0.25)
        struct.pack_into("<bH", car, 15, 7, 11800)
        car[18] = 1
        struct.pack_into("<4H", car, 22, 500, 501, 502, 503)
        struct.pack_into("<4f", car, 40, 20.1, 20.2, 23.1, 23.2)
        packet = header + bytes(car) + bytes(60 * 21)
        decoded_header = decode_header(packet)
        self.assertIsNotNone(decoded_header)
        telemetry = decode_player_car_telemetry(packet, decoded_header)
        self.assertEqual(HEADER_SIZE, 29)
        self.assertEqual(telemetry["speed_kph"], 301)
        self.assertEqual(telemetry["gear"], 7)
        self.assertEqual(telemetry["brake_temps_c"], (500, 501, 502, 503))
        self.assertEqual(telemetry["tyre_pressures_psi"], (20.1, 20.2, 23.1, 23.2))

    def test_setup_uses_the_documented_50_byte_stride(self):
        header = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 5, 42, 10.5, 17, 17, 1, 255)
        first_car = bytes(50)
        player_car = bytearray(50)
        player_car[0:4] = bytes([19, 17, 20, 45])
        struct.pack_into("<4f", player_car, 4, -3.5, -2.0, -0.05, 0.13)
        player_car[20:29] = bytes([33, 16, 14, 8, 21, 46, 100, 56, 70])
        struct.pack_into("<4f", player_car, 29, 21.1, 21.2, 24.1, 24.2)
        player_car[45] = 7
        struct.pack_into("<f", player_car, 46, 5.75)
        packet = header + first_car + player_car + bytes(50 * 20)
        setup = decode_player_setup(packet, decode_header(packet))
        self.assertEqual(setup["front_wing"], 19)
        self.assertEqual(setup["rear_wing"], 17)
        self.assertEqual(setup["front_suspension"], 33)
        self.assertEqual(setup["rear_ride_height"], 46)
        self.assertEqual(setup["brake_pressure"], 100)
        self.assertEqual(setup["brake_bias"], 56)
        self.assertEqual(setup["engine_braking"], 70)
        self.assertEqual(setup["ballast"], 7)
        for key, expected in {
            "front_camber": -3.5, "rear_camber": -2.0, "front_toe": -0.05, "rear_toe": 0.13,
            "rear_left_tyre_pressure_psi": 21.1, "rear_right_tyre_pressure_psi": 21.2,
            "front_left_tyre_pressure_psi": 24.1, "front_right_tyre_pressure_psi": 24.2,
            "fuel_load_kg": 5.75,
        }.items():
            self.assertAlmostEqual(setup[key], expected, places=5, msg=key)

    def test_session_packet_decodes_mode_track_and_length(self):
        header = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 1, 1, 9876, 10.5, 17, 17, 0, 255)
        body = bytearray(8)
        struct.pack_into("<H", body, 4, 5891)
        body[6] = 18
        struct.pack_into("<b", body, 7, 7)
        decoded = decode_session(header + body, decode_header(header + body))
        self.assertEqual(decoded, {
            "session_uid": 9876, "session_type": 18, "mode": "time_trial",
            "track_id": 7, "track_length_m": 5891,
        })

    def test_session_packet_rejects_wrong_format_or_version(self):
        wrong_format = struct.pack("<HBBBBBQfIIBB", 2024, 24, 1, 0, 1, 1, 1, 0, 0, 0, 0, 255) + bytes(8)
        wrong_version = struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 0, 2, 1, 1, 0, 0, 0, 0, 255) + bytes(8)
        self.assertIsNone(decode_session(wrong_format, decode_header(wrong_format)))
        self.assertIsNone(decode_session(wrong_version, decode_header(wrong_version)))


if __name__ == "__main__":
    unittest.main()
