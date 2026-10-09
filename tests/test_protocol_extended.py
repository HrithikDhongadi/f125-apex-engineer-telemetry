import struct
import unittest

from src.f1telemetry.protocol import (
    HEADER_SIZE, decode_car_damage, decode_car_status, decode_event, decode_header, decode_packet,
    decode_lap_positions, decode_motion, supported_header,
)


def header(packet_id: int, frame: int = 10, version: int = 1) -> bytes:
    return struct.pack("<HBBBBBQfIIBB", 2025, 25, 1, 7, version, packet_id, 99, 12.5, frame, frame, 0, 255)


class ExtendedProtocolTests(unittest.TestCase):
    def test_button_event_decodes_controller_status_bitmask(self):
        packet = header(3) + b"BUTN" + struct.pack("<I", 0x00200401)
        decoded = decode_event(packet, decode_header(packet))
        self.assertEqual(decoded["code"], "BUTN")
        self.assertEqual(decoded["button_status"], 0x00200401)

    def test_motion_offsets_and_length(self):
        body = bytearray(60 * 22)
        struct.pack_into("<6f6h6f", body, 0, 1.25, 2.5, -3.75, 4, 5, 6, 32767, 0, -32767, 0, 0, 0, 1.1, -0.5, 0.2, 2.2, 0.3, -0.4)
        packet = header(0) + body
        decoded = decode_motion(packet, decode_header(packet))
        self.assertEqual(len(packet), 1349)
        self.assertEqual(decoded["player"]["world_x"], 1.25)
        self.assertAlmostEqual(decoded["player"]["forward_z"], -1.0)
        self.assertAlmostEqual(decoded["player"]["yaw"], 2.2)

    def test_car_status_documented_offsets(self):
        body = bytearray(55 * 22)
        body[0:5] = bytes([2, 1, 1, 56, 1])
        struct.pack_into("<fff", body, 5, 31.5, 110.0, 14.25)
        body[25:29] = bytes([18, 16, 7, 3])
        struct.pack_into("<f", body, 37, 3_500_000.0)
        body[41] = 3
        struct.pack_into("<f", body, 50, 1_250_000.0)
        packet = header(7) + body
        decoded = decode_car_status(packet, decode_header(packet))["player"]
        self.assertEqual(len(packet), 1239)
        self.assertEqual(decoded["actual_tyre_compound"], 18)
        self.assertEqual(decoded["visual_tyre_compound"], 16)
        self.assertEqual(decoded["tyre_age_laps"], 7)
        self.assertEqual(decoded["fia_flag"], 3)
        self.assertEqual(decoded["ers_deploy_mode"], 3)

    def test_damage_wheel_order_and_lap_positions(self):
        damage_body = bytearray(46 * 22)
        struct.pack_into("<4f", damage_body, 0, 1, 2, 3, 4)
        damage_body[16:20] = bytes([5, 6, 7, 8])
        damage = header(10) + damage_body
        decoded = decode_car_damage(damage, decode_header(damage))["player"]
        self.assertEqual(len(damage), 1041)
        self.assertEqual(decoded["tyre_wear_pct"], [1, 2, 3, 4])
        self.assertEqual(decoded["tyre_damage_pct"], [5, 6, 7, 8])

        positions_body = bytearray(1102)
        positions_body[0:2] = bytes([2, 4])
        positions_body[2:24] = bytes(range(1, 23))
        positions = header(15) + positions_body
        result = decode_lap_positions(positions, decode_header(positions))
        self.assertEqual(len(positions), 1131)
        self.assertEqual(result["laps"][0]["lap_index"], 4)
        self.assertEqual(result["laps"][0]["positions"][0], 1)

    def test_unknown_packet_version_is_rejected(self):
        packet = header(0, version=2) + bytes(60 * 22)
        self.assertFalse(supported_header(decode_header(packet)))
        self.assertIsNone(decode_motion(packet, decode_header(packet)))

    def test_full_session_tail_decodes_sector_boundaries(self):
        raw = bytearray(header(1) + bytes(753 - HEADER_SIZE))
        raw[HEADER_SIZE] = 3
        struct.pack_into("<b", raw, HEADER_SIZE + 1, 41)
        struct.pack_into("<b", raw, HEADER_SIZE + 2, 28)
        raw[HEADER_SIZE + 3] = 52
        struct.pack_into("<H", raw, HEADER_SIZE + 4, 5890)
        raw[HEADER_SIZE + 6] = 15
        struct.pack_into("<b", raw, HEADER_SIZE + 7, 7)
        raw[HEADER_SIZE + 124] = 2
        struct.pack_into("<ff", raw, 745, 1850.5, 3920.25)
        decoded = decode_packet(bytes(raw), decode_header(raw))
        self.assertEqual(decoded["sector2_start_m"], 1850.5)
        self.assertEqual(decoded["sector3_start_m"], 3920.25)
        self.assertEqual(decoded["safety_car_status"], 2)

    def test_all_persisted_packet_types_accept_official_v1_lengths(self):
        sizes = {
            0: 1349, 1: 753, 2: 1285, 3: 45, 4: 1284, 5: 1133,
            6: 1352, 7: 1239, 8: 1042, 10: 1041, 11: 1460,
            12: 231, 13: 273, 15: 1131,
        }
        for packet_id, size in sizes.items():
            with self.subTest(packet_id=packet_id):
                raw = bytearray(header(packet_id) + bytes(size - HEADER_SIZE))
                if packet_id == 3:
                    raw[HEADER_SIZE:HEADER_SIZE + 4] = b"SSTA"
                decoded_header = decode_header(raw)
                self.assertIsNotNone(decode_packet(bytes(raw), decoded_header))


if __name__ == "__main__":
    unittest.main()
