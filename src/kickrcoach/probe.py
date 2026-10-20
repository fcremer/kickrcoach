"""M0 orchestration only: capabilities, concurrent telemetry and manual test."""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
import platform
import time
from typing import Any
from . import ble
from .control import TrainerControl
from .protocol import (FTMS, HRS, BIKE, FEATURE, POWER_RANGE, CONTROL, STATUS,
                       HEART, PacketError, PowerRange, features, heart_rate, indoor_bike)
from .safety import SafetyController
from .sensors import HeartRateState
from .simulator import SimulatedClient

log = logging.getLogger(__name__)


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                      "event": event, **fields}), flush=True)


class Probe:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.hr = HeartRateState(config.stale_sec, config.lost_sec)
        self.control: TrainerControl | None = None
        self.failed = asyncio.Event()
        self.stopping = False
        self.seen: dict[str, list[float]] = defaultdict(list)
        self.fields: set[str] = set()
        self.invalid: dict[str, int] = defaultdict(int)
        self.latest: dict[str, tuple[float, float | int]] = {}
        self.test_active = False
        self.hr_client: Any = None
        self.trainer: Any = None

    def disconnected(self, role: str) -> None:
        emit("disconnect", role=role, expected=self.stopping)
        if role == "hr":
            self.hr.connected = False
        if not self.stopping:
            self.failed.set()  # M0 needs continuous *both* connections; no auto-resume.
            if self.control:
                self.control.invalidate(f"{role} disconnected during M0")

    def receive(self, role: str, data: bytearray) -> None:
        if self.stopping:
            return
        now = time.monotonic()
        times = self.seen[role]
        interval = None if not times else now - times[-1]
        try:
            values = indoor_bike(data) if role == "trainer" else heart_rate(data)
        except PacketError as exc:
            self.invalid[role] += 1
            emit("invalid_packet", role=role, error=str(exc), hex=data.hex())
            return
        times.append(now)
        if role == "trainer":
            self.fields.update(values)
            for key, value in values.items():
                self.latest[key] = now, value
        else:
            self.hr.update(int(values["bpm"]), now, values["contact"])
        emit("telemetry", role=role, receive_monotonic=now,
             interval_sec=interval, **values)

    async def capabilities(self) -> tuple[PowerRange | None, bool]:
        supported = None
        target_supported = False
        for service in self.trainer.services:
            emit("gatt_service", uuid=service.uuid)
            for char in service.characteristics:
                emit("gatt_characteristic", uuid=char.uuid, properties=char.properties)
        for char, decoder in ((FEATURE, features), (POWER_RANGE, PowerRange.decode)):
            try:
                raw = bytes(await self.trainer.read_gatt_char(char))
                decoded = decoder(raw)
                if isinstance(decoded, PowerRange):
                    supported = decoded
                    value = vars(decoded)
                else:
                    value = decoded
                    target_supported = bool(decoded["power_target_supported"])
                emit("capability", uuid=char, hex=raw.hex(), value=value)
            except Exception as exc:
                # Optional characteristic/read failure does not block read-only telemetry.
                emit("capability_unavailable", uuid=char, error=type(exc).__name__)
        return supported, target_supported

    def require_property(self, char_uuid: str, prop: str, required: bool) -> bool:
        if not self.config.hardware:
            return True
        char = self.trainer.services.get_characteristic(char_uuid)
        if char is None or prop not in char.properties:
            emit("characteristic_unavailable", uuid=char_uuid, property=prop)
            if required:
                raise RuntimeError(f"required characteristic/property missing: {char_uuid} {prop}")
            return False
        return True

    async def run(self) -> bool:
        emit("environment", os=platform.platform(), python=platform.python_version(),
             simulated=not self.config.hardware)
        if self.config.hardware:
            found = await ble.discover(self.config.scan_sec)
            trainer_device = ble.select(found, self.config.trainer_id, FTMS)
            hr_device = ble.select(found, self.config.hr_id, HRS)
            self.trainer = ble.make_client(trainer_device, lambda _: self.disconnected("trainer"))
            self.hr_client = ble.make_client(hr_device, lambda _: self.disconnected("hr"))
        else:
            self.trainer = SimulatedClient("trainer", self.config.scenario,
                                           lambda _: self.disconnected("trainer"))
            self.hr_client = SimulatedClient("hr", self.config.scenario,
                                             lambda _: self.disconnected("hr"))
        try:
            # Both tasks settle before cleanup, including one-sided connection failure.
            results = await asyncio.gather(self.trainer.connect(), self.hr_client.connect(),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            if self.failed.is_set() or not all(c.is_connected for c in (self.trainer, self.hr_client)):
                raise RuntimeError("connection lost during setup")
            self.hr.connected = True
            emit("connected", roles=["trainer", "hr"])
            supported, target_supported = await self.capabilities()
            self.control = TrainerControl(self.trainer, SafetyController(self.config.maximum_w),
                                          supported, self.config.command_timeout_sec)
            self.require_property(BIKE, "notify", True)
            await self.trainer.start_notify(BIKE, lambda _, data: self.receive("trainer", data))
            await self.hr_client.start_notify(HEART, lambda _, data: self.receive("hr", data))
            if self.require_property(STATUS, "notify", False):
                await self.subscribe_optional(STATUS, self.status)
            if self.require_property(CONTROL, "indicate", self.config.control_test):
                self.require_property(CONTROL, "write", self.config.control_test)
                await self.subscribe_optional(CONTROL, self.control.indication)
            started = time.monotonic()
            if self.config.control_test:
                if not target_supported:
                    raise RuntimeError("target-power feature not confirmed; refusing test")
                self.control.safety.validate(self.config.target_w, supported)
                # Do not command load before both live sensors and pedalling are seen.
                deadline = time.monotonic() + self.config.lost_sec
                while not self.ready_for_control():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("live HR, power and positive cadence required for manual test")
                    await self.monitor(0.2)
                await self.control.request_control()
                await self.control.set_target_power(self.config.target_w)
                await self.control.start()
                self.test_active = True
                await self.monitor(self.config.test_sec)
                await self.control.stop()
                self.test_active = False
                emit("manual_test_finished", protocol_acknowledged=True,
                     physical_verification="REQUIRED: observe trainer and telemetry locally")
            remaining = max(0, self.config.duration_sec - (time.monotonic() - started))
            await self.monitor(remaining)
            duration = time.monotonic() - started
            adequate = (duration >= 600 and self.seen["trainer"] and self.seen["hr"]
                        and {"power_w", "cadence_rpm"}.issubset(self.fields)
                        and self.hr.snapshot(time.monotonic())["state"] == "LIVE"
                        and not self.failed.is_set()
                        and not any(self.invalid.values()))
            for role in ("trainer", "hr"):
                times = self.seen[role]
                gaps = [b - a for a, b in zip([started] + times, times + [time.monotonic()])]
                max_gap = max(gaps)
                adequate = adequate and max_gap < self.config.lost_sec
                emit("notification_summary", role=role, samples=len(times),
                     duration_sec=duration, max_gap_sec=max_gap,
                     invalid_packets=self.invalid[role])
            emit("probe_result", telemetry_10min_observed=bool(adequate),
                 simulated=not self.config.hardware, observed_fields=sorted(self.fields),
                 hardware_acceptance="NOT AUTOMATIC: physical test + report review required")
            # Short probes are useful smoke tests, not acceptance evidence.
            return bool(not self.failed.is_set() and self.ready_for_control(pedalling=False)
                        and not any(self.invalid.values()) and (duration < 600 or adequate))
        except Exception as exc:
            # Our errors contain no device identifiers; backend error strings are not emitted.
            emit("probe_failed", category=type(exc).__name__,
                 reason=str(exc) if type(exc) in (RuntimeError, ValueError) else "see protocol events")
            raise
        finally:
            await self.cleanup()

    async def subscribe_optional(self, characteristic: str, callback: Any) -> None:
        try:
            await self.trainer.start_notify(characteristic, callback)
        except Exception as exc:
            emit("subscription_failed", uuid=characteristic, category=type(exc).__name__)
            if self.config.control_test:
                raise RuntimeError("cannot monitor FTMS control/status; refusing manual test") from exc

    def status(self, _sender: object, data: bytearray) -> None:
        emit("ftms_status", hex=data.hex())
        if data and data[0] in (0x01, 0xFF):  # Reset / Control Permission Lost
            if self.control:
                self.control.invalidate("FTMS reset/control permission lost")
            if self.config.control_test:
                self.failed.set()

    def ready_for_control(self, pedalling: bool = True) -> bool:
        now = time.monotonic()
        return (self.hr.snapshot(now)["state"] == "LIVE"
                and all(key in self.latest and now - self.latest[key][0] < self.config.stale_sec
                        for key in ("power_w", "cadence_rpm"))
                and (not pedalling or self.latest["cadence_rpm"][1] > 0))

    async def monitor(self, duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if self.failed.is_set():
                raise RuntimeError("BLE/control state lost; probe aborted; no auto-reconnect")
            if self.test_active and not self.ready_for_control():
                raise RuntimeError("manual test interrupted: telemetry stale, HR lost or cadence zero")
            emit("hr_freshness", **self.hr.snapshot(time.monotonic()))
            try:
                await asyncio.wait_for(self.failed.wait(), timeout=min(1, end-time.monotonic()))
            except TimeoutError:
                pass
        if self.failed.is_set():
            raise RuntimeError("BLE/control state lost")

    async def cleanup(self) -> None:
        self.stopping = True
        if self.control and self.control.attempted:
            try:
                await self.control.stop()
                emit("stop_acknowledged")
            except Exception as exc:
                emit("stop_failed", error=type(exc).__name__,
                     action="Physical stop/unload required; do not assume resistance released")
        # FTMS has no Release Control opcode. Stop then disconnect; no previous load restore.
        for role, client in (("hr", self.hr_client), ("trainer", self.trainer)):
            if client:
                try:
                    async with asyncio.timeout(10):
                        await client.disconnect()
                    emit("disconnected", role=role)
                except Exception as exc:
                    emit("disconnect_failed", role=role, error=type(exc).__name__)
        self.hr.connected = False
