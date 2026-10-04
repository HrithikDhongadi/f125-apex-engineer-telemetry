import struct
import unittest

from src.f1telemetry.protocol import (
    HEADER_SIZE,
    decode_header,
    decode_player_car_telemetry,
    decode_player_setup,
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
        player_car = bytes([19, 15, 100, 30]) + bytes(46)
        packet = header + first_car + player_car + bytes(50 * 20)
        setup = decode_player_setup(packet, decode_header(packet))
        self.assertEqual(setup, {"front_wing": 19, "rear_wing": 15, "on_throttle_diff": 100, "off_throttle_diff": 30})


if __name__ == "__main__":
    unittest.main()
