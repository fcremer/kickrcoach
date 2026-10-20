import asyncio
import unittest
from kickrcoach.control import TrainerControl, ControlError
from kickrcoach.protocol import PowerRange, CONTROL
from kickrcoach.safety import SafetyController
from kickrcoach.simulator import SimulatedClient


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def setup_control(self, scenario="normal"):
        client = SimulatedClient("trainer", scenario)
        await client.connect()
        self.addAsyncCleanup(client.disconnect)
        control = TrainerControl(client, SafetyController(), PowerRange(0, 1000, 1), 0.02)
        await client.start_notify(CONTROL, control.indication)
        return client, control

    async def test_request_target_start_stop(self):
        client, control = await self.setup_control()
        with self.assertRaises(ControlError):
            await control.set_target_power(50)
        self.assertEqual(client.writes, [])
        await control.request_control()
        await control.set_target_power(50)
        await control.start()
        await control.stop()
        self.assertEqual(client.writes, [b"\x00", b"\x05\x32\x00", b"\x07", b"\x08\x01"])
        self.assertFalse(control.owned)

    async def test_rejection_invalidates_control(self):
        client, control = await self.setup_control("rejected-control")
        with self.assertRaisesRegex(ControlError, "rejected"):
            await control.request_control()
        with self.assertRaises(ControlError):
            await control.set_target_power(50)
        self.assertFalse(control.owned)
        self.assertEqual(client.writes, [b"\x00"])
        await control.stop()

    async def test_write_success_is_not_ftms_success(self):
        client, control = await self.setup_control("timeout")
        with self.assertRaises(TimeoutError):
            await control.request_control()
        control.indication(None, bytearray([0x80, 0, 1]))  # late indication cannot restore ownership
        self.assertFalse(control.owned)
        with self.assertRaises(ControlError):
            await control.request_control()
        await control.stop()

    async def test_disconnect_invalidates_pending_request(self):
        client, control = await self.setup_control("timeout")
        task = asyncio.create_task(control.request_control())
        await asyncio.sleep(0)
        client.is_connected = False
        control.invalidate("disconnect")
        with self.assertRaises(ControlError):
            await task
        self.assertFalse(control.owned)

    async def test_cancellation_and_invalid_target(self):
        client, control = await self.setup_control()
        await control.request_control()
        with self.assertRaises(ValueError):
            await control.set_target_power(500)
        self.assertEqual(client.writes, [b"\x00"])
        client.scenario = "timeout"
        task = asyncio.create_task(control.set_target_power(50))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(control.owned)
        await control.stop()

    async def test_serialization_and_wrong_response(self):
        client, control = await self.setup_control()
        await control.request_control()
        active = 0
        maximum = 0

        async def write(char, packet, response=True):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            control.indication(None, bytearray([0x80, 7, 1]))
            await asyncio.sleep(0.001)
            control.indication(None, bytearray([0x80, packet[0], 1]))
            active -= 1

        client.write_gatt_char = write
        await asyncio.gather(control.set_target_power(50), control.set_target_power(60))
        self.assertEqual(maximum, 1)
