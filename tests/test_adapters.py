import asyncio
import contextlib
from datetime import datetime, timezone
import unittest
from kickrcoach.domain import (TrainerAdapter, HeartRateSource, TrainerSample, HeartRateSample,
                              ConnectionState as CS, ControlState as TS, Freshness)
from kickrcoach.heart_rate import BleHeartRateSource
from kickrcoach.simulation import (SimulatedTrainer, SimulatedHeartRateSource, MemoryTransport,
                                  ManualClock, Faults)
from kickrcoach.trainer import FtmsTrainer, AdapterStateError
from kickrcoach.control import ControlError
from kickrcoach.protocol import BIKE, CONTROL, FEATURE, POWER_RANGE, STATUS, HEART


def drain(source, kind):
    items = []
    while not source.events.empty():
        event = source.events.get_nowait()
        source.events.task_done()
        if isinstance(event, kind):
            items.append(event)
    return items


def memory_factory(role, customize=lambda t: None):
    transports = []

    def factory(on_disconnect):
        transport = MemoryTransport(role, on_disconnect, Faults())
        customize(transport)
        transports.append(transport)
        return transport
    return factory, transports


class AdapterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_and_simulated_trainer_same_contract(self):
        factory, transports = memory_factory("trainer")
        for trainer in (FtmsTrainer(factory), SimulatedTrainer()):
            with self.subTest(adapter=type(trainer).__name__):
                self.assertIsInstance(trainer, TrainerAdapter)
                await trainer.connect()
                transport = trainer.transport if isinstance(trainer, SimulatedTrainer) else transports[-1]
                self.assertEqual(transport.writes, [])
                self.assertEqual(trainer.control_state, TS.READONLY)
                self.assertTrue((await trainer.capabilities()).can_set_power)
                await trainer.request_control()
                self.assertEqual(trainer.control_state, TS.READY)
                with self.assertRaises(AdapterStateError):
                    await trainer.resume()
                await trainer.set_target_power(50)
                await trainer.resume()
                self.assertEqual(trainer.control_state, TS.RUNNING)
                await trainer.pause()
                self.assertEqual(trainer.control_state, TS.PAUSED)
                await trainer.resume()
                await trainer.stop()
                self.assertEqual(trainer.control_state, TS.READONLY)
                self.assertIsNone(trainer.target_power_w)
                await trainer.disconnect()
                self.assertFalse(transport.is_connected)
                self.assertEqual(trainer.connection, CS.DISCONNECTED)

    async def test_real_and_simulated_hr_same_contract_and_clock(self):
        clock = ManualClock()
        factory, transports = memory_factory("hr")
        for source in (BleHeartRateSource(factory, clock=clock), SimulatedHeartRateSource(clock=clock)):
            self.assertIsInstance(source, HeartRateSource)
            await source.connect()
            self.assertEqual(source.latest.freshness, Freshness.LOST)
            transport = source.transport if isinstance(source, SimulatedHeartRateSource) else transports[-1]
            transport.notify(HEART, bytes.fromhex("0078"))
            self.assertEqual(source.latest.bpm, 120)
            received = source.latest.received
            self.assertIsNotNone(received.utc.tzinfo)
            clock.advance(5)
            self.assertEqual(source.refresh().freshness, Freshness.STALE)
            self.assertIsNone(source.latest.bpm)
            clock.advance(10)
            self.assertEqual(source.refresh().freshness, Freshness.LOST)
            self.assertEqual(source.latest.received, received)
            transport.notify(HEART, bytes.fromhex("017900"))
            self.assertEqual(source.latest.bpm, 121)
            await source.disconnect()
            self.assertIsNone(source.latest.bpm)

    async def test_packet_fields_are_not_carried_forward(self):
        trainer = SimulatedTrainer(clock=ManualClock())
        await trainer.connect()
        trainer.emit_sample(power_w=60, cadence_rpm=80.5)
        trainer.transport.notify(BIKE, b"\x01\x00")
        samples = drain(trainer, TrainerSample)
        self.assertEqual(samples[0].power_w, 60)
        self.assertEqual(samples[0].cadence_rpm, 80.5)
        self.assertIsNone(samples[1].power_w)
        self.assertIsNone(samples[1].cadence_rpm)
        self.assertIsNone(samples[0].received.source_utc)
        await trainer.disconnect()

    async def test_concurrent_hr_loss_does_not_stop_trainer(self):
        trainer, hr = SimulatedTrainer(), SimulatedHeartRateSource()
        await asyncio.gather(trainer.connect(), hr.connect())
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        hr.lose_connection()
        self.assertEqual(hr.latest.freshness, Freshness.LOST)
        self.assertEqual(trainer.control_state, TS.RUNNING)
        self.assertEqual(trainer.target_power_w, 50)
        await asyncio.gather(trainer.disconnect(), hr.disconnect())

    async def test_hr_invalid_contact_and_old_session_samples(self):
        clock = ManualClock()
        hr = SimulatedHeartRateSource(clock=clock)
        await hr.connect()
        old_callback = hr.transport.callbacks[HEART]
        hr.emit_sample()
        clock.advance(5)
        hr.transport.notify(HEART, b"\x01")
        self.assertEqual(hr.refresh().freshness, Freshness.STALE)
        hr.transport.notify(HEART, b"\x04\x78")
        self.assertEqual(hr.latest.freshness, Freshness.LOST)
        await hr.disconnect()
        await hr.connect()
        old_callback(b"\x00\x79")
        self.assertEqual(hr.latest.freshness, Freshness.LOST)
        hr.emit_sample(130)
        self.assertEqual(hr.latest.bpm, 130)
        await hr.disconnect()

    async def test_hr_freshness_events_without_new_packets(self):
        hr = SimulatedHeartRateSource(stale_sec=.01, lost_sec=.02)
        await hr.connect()
        hr.emit_sample()
        await asyncio.sleep(.04)
        self.assertEqual(hr.latest.freshness, Freshness.LOST)
        states = [e.freshness for e in drain(hr, HeartRateSample)]
        self.assertIn(Freshness.LIVE, states)
        self.assertIn(Freshness.STALE, states)
        self.assertEqual(states[-1], Freshness.LOST)
        await hr.disconnect()
        self.assertIsNone(hr._watcher)

    async def test_bounded_event_buffer_reports_drops(self):
        trainer = SimulatedTrainer(event_capacity=3)
        await trainer.connect()
        for _ in range(10):
            trainer.emit_sample()
        self.assertEqual(trainer.events.qsize(), 3)
        self.assertGreater(trainer.dropped_events, 0)
        await trainer.disconnect()


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejection_and_timeout_are_not_success(self):
        for faults, error in ((Faults(rejected_control=True), ControlError),
                              (Faults(timeout=True), TimeoutError)):
            trainer = SimulatedTrainer(faults=faults, command_timeout_sec=.01)
            await trainer.connect()
            with self.assertRaises(error):
                await trainer.request_control()
            self.assertEqual(trainer.connection, CS.DEGRADED)
            self.assertEqual(trainer.control_state, TS.UNCERTAIN)
            with self.assertRaises(AdapterStateError):
                await trainer.set_target_power(50)
            await trainer.disconnect()
            self.assertEqual(trainer.connection, CS.DISCONNECTED)

    async def test_target_timeout_and_caller_cancellation_clear_control(self):
        for cancel in (False, True):
            trainer = SimulatedTrainer(command_timeout_sec=.01)
            await trainer.connect()
            await trainer.request_control()
            trainer.faults.timeout = True
            target = asyncio.create_task(trainer.set_target_power(50))
            if cancel:
                await asyncio.sleep(0)
                target.cancel()
            with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                await target
            self.assertEqual(trainer.control_state, TS.UNCERTAIN)
            self.assertIsNone(trainer.target_power_w)
            await trainer.disconnect()

    async def test_stop_preempts_command_and_discards_queued_target(self):
        trainer = SimulatedTrainer(command_timeout_sec=1)
        await trainer.connect()
        await trainer.request_control()
        trainer.faults.timeout = True
        first = asyncio.create_task(trainer.set_target_power(50))
        await asyncio.sleep(0)
        queued = asyncio.create_task(trainer.set_target_power(60))
        await asyncio.sleep(0)
        await trainer.stop()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(AdapterStateError):
            await queued
        self.assertEqual(trainer.transport.writes, [b"\x00", b"\x05\x32\x00", b"\x08\x01"])
        self.assertIsNone(trainer.target_power_w)
        self.assertEqual(trainer.control_state, TS.UNCERTAIN)
        await trainer.disconnect()

    async def test_reconnect_has_no_automatic_commands_or_previous_load(self):
        trainer = SimulatedTrainer()
        await trainer.connect()
        await trainer.request_control()
        await trainer.set_target_power(80)
        await trainer.resume()
        old_transport = trainer.transport
        old_response = old_transport.callbacks[CONTROL]
        old_sample = old_transport.callbacks[BIKE]
        trainer.lose_connection()
        self.assertEqual(trainer.connection, CS.DISCONNECTED)
        self.assertIsNone(trainer.target_power_w)
        await trainer.disconnect()
        await trainer.connect()
        self.assertEqual(trainer.transport.writes, [])
        self.assertEqual(trainer.control_state, TS.READONLY)
        old_response(b"\x80\x00\x01")
        old_sample(b"\x41\x00\x50\x00")
        self.assertEqual(trainer.control_state, TS.READONLY)
        with self.assertRaises(AdapterStateError):
            await trainer.resume()
        await trainer.request_control()
        with self.assertRaises(AdapterStateError):
            await trainer.resume()
        await trainer.set_target_power(50)
        await trainer.resume()
        await trainer.disconnect()

    async def test_disconnect_during_pending_control(self):
        trainer = SimulatedTrainer(command_timeout_sec=1)
        await trainer.connect()
        trainer.faults.timeout = True
        task = asyncio.create_task(trainer.request_control())
        await asyncio.sleep(.001)
        trainer.lose_connection()
        with self.assertRaises(ControlError):
            await task
        self.assertEqual(trainer.connection, CS.DISCONNECTED)
        self.assertEqual(trainer.control_state, TS.UNAVAILABLE)
        await trainer.disconnect()

    async def test_reset_and_control_permission_loss(self):
        for packet in (b"\x01", b"\xff"):
            trainer = SimulatedTrainer()
            await trainer.connect()
            await trainer.request_control()
            trainer.transport.notify(STATUS, packet)
            self.assertEqual(trainer.control_state, TS.UNCERTAIN)
            with self.assertRaises(AdapterStateError):
                await trainer.set_target_power(50)
            await trainer.disconnect()

    async def test_missing_optional_range_disables_only_power_control(self):
        def customize(transport):
            transport.reads_failed.add(POWER_RANGE)
            transport.subscriptions_failed.add(STATUS)
        factory, transports = memory_factory("trainer", customize)
        trainer = FtmsTrainer(factory)
        await trainer.connect()
        caps = await trainer.capabilities()
        self.assertTrue(caps.can_control)
        self.assertFalse(caps.can_set_power)
        self.assertFalse(caps.status_subscribed)
        await trainer.request_control()
        with self.assertRaises(AdapterStateError):
            await trainer.set_target_power(50)
        self.assertEqual(transports[-1].writes, [b"\x00"])
        await trainer.disconnect()

    async def test_control_subscription_failure_is_readonly(self):
        factory, transports = memory_factory("trainer", lambda t: t.subscriptions_failed.add(CONTROL))
        trainer = FtmsTrainer(factory)
        await trainer.connect()
        self.assertFalse((await trainer.capabilities()).can_control)
        with self.assertRaises(AdapterStateError):
            await trainer.request_control()
        await trainer.disconnect()
        self.assertEqual(transports[-1].writes, [])

    async def test_required_subscription_failure_cleans_up(self):
        for role, adapter, uuid in (("trainer", FtmsTrainer, BIKE), ("hr", BleHeartRateSource, HEART)):
            factory, transports = memory_factory(role, lambda t: t.subscriptions_failed.add(uuid))
            source = adapter(factory)
            with self.assertRaises(OSError):
                await source.connect()
            self.assertEqual(source.connection, CS.DISCONNECTED)
            self.assertFalse(transports[-1].is_connected)

    async def test_invalid_target_preserves_state_and_never_writes(self):
        trainer = SimulatedTrainer()
        await trainer.connect()
        await trainer.request_control()
        with self.assertRaises(ValueError):
            await trainer.set_target_power(500)
        self.assertEqual(trainer.control_state, TS.READY)
        self.assertEqual(trainer.transport.writes, [b"\x00"])
        await trainer.disconnect()

    async def test_serialized_target_writes(self):
        trainer = SimulatedTrainer(faults=Faults(response_delay_sec=.001))
        await trainer.connect()
        await trainer.request_control()
        await asyncio.gather(trainer.set_target_power(50), trainer.set_target_power(60))
        self.assertEqual(trainer.target_power_w, 60)
        self.assertEqual(trainer.control_state, TS.READY)
        await trainer.disconnect()

    async def test_pause_status_does_not_replace_indication(self):
        trainer = SimulatedTrainer()
        await trainer.connect()
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        write = trainer.transport.write_gatt_char

        async def with_status(char, packet, response=True):
            if packet == b"\x08\x02":
                trainer.transport.notify(STATUS, b"\x02\x02")
            await write(char, packet, response)
        trainer.transport.write_gatt_char = with_status
        await trainer.pause()
        self.assertEqual(trainer.control_state, TS.PAUSED)
        await trainer.disconnect()

    async def test_ambiguous_pause_timeout_cannot_acknowledge_later_stop(self):
        trainer = SimulatedTrainer(command_timeout_sec=.01)
        await trainer.connect()
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        write = trainer.transport.write_gatt_char

        async def silent_pause(char, packet, response=True):
            if packet == b"\x08\x02":
                trainer.transport.writes.append(packet)
                return
            await write(char, packet, response)
        trainer.transport.write_gatt_char = silent_pause
        with self.assertRaises(TimeoutError):
            await trainer.pause()
        trainer.transport.notify(CONTROL, b"\x80\x08\x01")
        with self.assertRaises(ControlError):
            await trainer.stop()
        self.assertEqual(trainer.control_state, TS.UNCERTAIN)
        self.assertNotIn(b"\x08\x01", trainer.transport.writes)
        await trainer.disconnect()


class AdditionalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_pause_status_can_arrive_after_control_response(self):
        trainer = SimulatedTrainer()
        await trainer.connect()
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        await trainer.pause()
        trainer.transport.notify(STATUS, b"\x02\x02")
        self.assertEqual(trainer.target_power_w, 50)
        await trainer.resume()
        await trainer.disconnect()

    async def test_stop_is_idempotent_and_readonly_disconnect_never_writes(self):
        trainer = SimulatedTrainer()
        await trainer.connect()
        await trainer.stop()
        await trainer.disconnect()
        self.assertEqual(trainer.transport.writes, [])
        await trainer.connect()
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        await trainer.stop()
        await trainer.stop()
        await trainer.disconnect()
        self.assertEqual(trainer.transport.writes.count(b"\x08\x01"), 1)

    async def test_cadence_collapse_is_telemetry_not_m2_workout_policy(self):
        trainer = SimulatedTrainer(faults=Faults(cadence_collapse=True))
        await trainer.connect()
        trainer.emit_sample()
        self.assertEqual(drain(trainer, TrainerSample)[0].cadence_rpm, 0)
        self.assertEqual(trainer.transport.writes, [])
        await trainer.disconnect()

    async def test_cancelled_setup_disconnects_and_can_retry(self):
        for role, adapter in (("trainer", FtmsTrainer), ("hr", BleHeartRateSource)):
            factory, transports = memory_factory(role)
            base_factory = factory

            def slow_factory(callback):
                transport = base_factory(callback)
                async def slow_connect():
                    transport.is_connected = True
                    await asyncio.Event().wait()
                transport.connect = slow_connect
                return transport

            source = adapter(slow_factory)
            task = asyncio.create_task(source.connect())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(transports[-1].is_connected)
            self.assertEqual(source.connection, CS.DISCONNECTED)

    async def test_hr_reconnect_does_not_revive_old_value(self):
        source = SimulatedHeartRateSource()
        await source.connect()
        source.emit_sample(140)
        source.lose_connection()
        self.assertIsNone(source.latest.bpm)
        await source.disconnect()
        await source.connect()
        self.assertIsNone(source.latest.bpm)
        await source.disconnect()

    async def test_wall_clock_change_does_not_change_hr_freshness(self):
        from datetime import timedelta
        clock = ManualClock()
        source = SimulatedHeartRateSource(clock=clock)
        await source.connect()
        source.emit_sample()
        clock.start += timedelta(days=1)
        self.assertEqual(source.latest.freshness, Freshness.LIVE)
        clock.advance(5)
        self.assertEqual(source.refresh().freshness, Freshness.STALE)
        await source.disconnect()

    async def test_bleak_wrapper_forwards_only_transport_values(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from kickrcoach.ble import BleakTransport
        fake = MemoryTransport("hr", lambda: None, Faults())
        fake.services = [SimpleNamespace(uuid="service", characteristics=[
            SimpleNamespace(uuid=HEART, properties=["notify"])])]

        async def start_notify(characteristic, callback):
            await fake.subscribe(characteristic, lambda data: callback(None, bytearray(data)))
        fake.start_notify = start_notify
        with patch("kickrcoach.ble.make_client", return_value=fake):
            transport = BleakTransport("opaque-platform-id", lambda: None)
        await transport.connect()
        received = []
        await transport.subscribe(HEART, received.append)
        fake.notify(HEART, b"\x00\x78")
        self.assertEqual(received, [b"\x00\x78"])
        self.assertEqual(transport.characteristics[0].uuid, HEART)
        await transport.disconnect()

    async def test_m1_demo_is_executable_without_bluetooth(self):
        import io
        from unittest.mock import patch
        from kickrcoach.demo import run
        with patch("kickrcoach.ble.make_client", side_effect=AssertionError("real BLE used")), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(await run(20, "stale-hr"), 0)
        self.assertIn('"freshness": "LOST"', out.getvalue())
        self.assertIn('"simulated": true', out.getvalue())

    async def test_optional_capability_timeout_does_not_break_readonly_telemetry(self):
        def customize(transport):
            read = transport.read_gatt_char
            async def timeout_range(characteristic):
                if characteristic == POWER_RANGE:
                    await asyncio.Event().wait()
                return await read(characteristic)
            transport.read_gatt_char = timeout_range
        factory, transports = memory_factory("trainer", customize)
        trainer = FtmsTrainer(factory, io_timeout_sec=.1)
        await trainer.connect()
        self.assertEqual(trainer.connection, CS.CONNECTED)
        self.assertFalse((await trainer.capabilities()).can_set_power)
        await trainer.disconnect()

    async def test_failed_disconnect_is_visible_and_retryable(self):
        for source in (SimulatedTrainer(), SimulatedHeartRateSource()):
            await source.connect()
            transport = source.transport
            disconnect = transport.disconnect
            async def fail_disconnect():
                raise OSError("disconnect failed")
            transport.disconnect = fail_disconnect
            with self.assertRaises(OSError):
                await source.disconnect()
            self.assertEqual(source.connection, CS.DEGRADED)
            self.assertTrue(transport.is_connected)
            with self.assertRaises(RuntimeError):
                await source.connect()
            transport.disconnect = disconnect
            await source.disconnect()
            self.assertEqual(source.connection, CS.DISCONNECTED)
            self.assertFalse(transport.is_connected)
