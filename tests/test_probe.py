import asyncio
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from kickrcoach.cli import Config, load, parser
from kickrcoach.probe import Probe
from kickrcoach.protocol import PowerRange, CONTROL
from kickrcoach.control import TrainerControl
from kickrcoach.safety import SafetyController
from kickrcoach.simulator import SimulatedClient


class ConfigTests(unittest.TestCase):
    def test_bad_config(self):
        for config in (Config(duration_sec=float("nan")), Config(lost_sec=3),
                       Config(target_w=200), Config(test_sec=31), Config(maximum_w=True)):
            with self.assertRaises(ValueError):
                config.validate()

    def test_config_cannot_enable_hardware(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text("hardware = true")
            with self.assertRaises(ValueError):
                load(parser().parse_args(["--config", str(path)]))
        self.assertFalse(load(parser().parse_args([])).hardware)
        with self.assertRaises(ValueError):
            load(parser().parse_args(["--hardware"]))


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_end_to_end_no_ble(self):
        probe = Probe(Config(duration_sec=1.2, test_sec=0.1, control_test=True))
        with patch("kickrcoach.ble.make_client", side_effect=AssertionError("BLE touched")), \
             patch("kickrcoach.ble.discover", side_effect=AssertionError("BLE touched")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await probe.run())
        self.assertFalse(probe.trainer.is_connected)
        self.assertFalse(probe.hr_client.is_connected)
        self.assertIn(b"\x05\x32\x00", probe.trainer.writes)
        self.assertEqual(probe.trainer.writes[-1], b"\x08\x01")

    async def test_readonly_never_writes(self):
        probe = Probe(Config(duration_sec=1.1))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await probe.run())
        self.assertEqual(probe.trainer.writes, [])

    async def test_rejected_control_still_disconnects(self):
        probe = Probe(Config(duration_sec=1.2, control_test=True, scenario="rejected-control"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            await probe.run()
        self.assertFalse(probe.trainer.is_connected)
        self.assertFalse(probe.hr_client.is_connected)
        self.assertFalse(any(packet[0] == 5 for packet in probe.trainer.writes))

    async def test_cancellation_stops_and_disconnects(self):
        probe = Probe(Config(duration_sec=600, control_test=True))
        with contextlib.redirect_stdout(io.StringIO()):
            task = asyncio.create_task(probe.run())
            await asyncio.sleep(1.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(probe.trainer.writes[-1], b"\x08\x01")
        self.assertFalse(probe.trainer.is_connected)
        self.assertFalse(probe.hr_client.is_connected)

    async def test_zero_cadence_interrupts_manual_test(self):
        probe = Probe(Config(duration_sec=6, test_sec=5, control_test=True,
                             scenario="cadence-collapse"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "cadence"):
            await probe.run()
        self.assertEqual(probe.trainer.writes[-1], b"\x08\x01")
        self.assertFalse(probe.trainer.is_connected)

    async def test_disconnect_does_not_reconnect(self):
        probe = Probe(Config(duration_sec=5, scenario="disconnect"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            await probe.run()
        self.assertEqual(probe.trainer.writes, [])
        self.assertFalse(probe.hr_client.is_connected)

    async def test_stale_hr_during_manual_test_stops(self):
        probe = Probe(Config(duration_sec=6, test_sec=5, control_test=True,
                             stale_sec=1.5, lost_sec=3, scenario="stale-hr"))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "telemetry stale"):
            await probe.run()
        self.assertEqual(probe.trainer.writes[-1], b"\x08\x01")

    async def test_cleanup_stop_failure_does_not_block_disconnect(self):
        probe = Probe(Config())
        probe.trainer = SimulatedClient("trainer")
        probe.hr_client = SimulatedClient("hr")
        await probe.trainer.connect()
        await probe.hr_client.connect()
        probe.control = TrainerControl(probe.trainer, SafetyController(), PowerRange(0, 100, 1), .01)
        probe.control.attempted = True  # no response callback -> stop failure
        with contextlib.redirect_stdout(io.StringIO()) as out:
            await probe.cleanup()
        self.assertIn("stop_failed", out.getvalue())
        self.assertFalse(probe.hr_client.is_connected)
        self.assertFalse(probe.trainer.is_connected)
