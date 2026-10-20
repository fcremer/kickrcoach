import struct
import unittest
from kickrcoach.protocol import indoor_bike, heart_rate, PowerRange, features, PacketError
from kickrcoach.sensors import HeartRateState
from kickrcoach.safety import SafetyController


class ProtocolTests(unittest.TestCase):
    def test_bike_signed_power_and_half_cadence(self):
        data = bytes.fromhex("4400d007a100f6ff")
        self.assertEqual(indoor_bike(data), {"speed_kmh": 20, "cadence_rpm": 80.5, "power_w": -10})

    def test_more_data_speed_absent(self):
        self.assertEqual(indoor_bike(bytes.fromhex("41007b00")), {"power_w": 123})
        self.assertEqual(indoor_bike(bytes.fromhex("0100")), {})

    def test_all_optional_offsets(self):
        data = (struct.pack("<HHHHH", 0x1FFE, 1000, 900, 161, 150)
                + b"\x00" * 3 + struct.pack("<hh", 10, 130) + b"\x00" * 13)
        result = indoor_bike(data)
        self.assertEqual(result["power_w"], 130)
        self.assertEqual(result["cadence_rpm"], 80.5)
        for end in range(len(data)):
            with self.assertRaises(PacketError):
                indoor_bike(data[:end])

    def test_hr_8_16_energy_rr_contact(self):
        self.assertEqual(heart_rate(bytes.fromhex("0078"))["bpm"], 120)
        result = heart_rate(bytes.fromhex("1f2c010a0000040002"))
        self.assertEqual(result, {"bpm": 300, "contact": True, "rr_sec": [1, 0.5]})
        self.assertFalse(heart_rate(bytes.fromhex("0478"))["contact"])
        for packet in (b"", b"\x01\x78", b"\x10\x78", b"\x10\x78\x01", b"\x00\x00"):
            with self.assertRaises(PacketError):
                heart_rate(packet)

    def test_capabilities_and_safety(self):
        limits = PowerRange.decode(bytes.fromhex("0000e8030500"))
        self.assertEqual(SafetyController().validate(50, limits), 50)
        for target in (True, 0, 51, 101, -1):
            with self.assertRaises(ValueError):
                SafetyController().validate(target, limits)
        with self.assertRaises(ValueError):
            SafetyController().validate(50, None)
        self.assertTrue(features(bytes.fromhex("0240000008000000"))["power_target_supported"])
        for packet in (b"", b"\x00" * 6):
            with self.assertRaises(PacketError):
                PowerRange.decode(packet)

    def test_freshness_never_reuses_stale_bpm(self):
        hr = HeartRateState(5, 15, connected=True)
        self.assertEqual(hr.snapshot(0)["state"], "LOST")
        hr.update(120, 10)
        self.assertEqual(hr.snapshot(14)["bpm"], 120)
        self.assertEqual(hr.snapshot(15), {"state": "STALE", "bpm": None, "receive_age_sec": 5})
        self.assertEqual(hr.snapshot(25)["state"], "LOST")
        hr.update(120, 26, False)
        self.assertEqual(hr.snapshot(26)["state"], "LOST")
        hr.update(120, 26)
        hr.connected = False
        self.assertIsNone(hr.snapshot(27)["bpm"])
