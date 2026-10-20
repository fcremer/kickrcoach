"""M1 deterministic adapters using real parsers/control code and no BLE imports.

Call emit_sample()/advance an injected clock; no real-time trainer task is needed.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import asyncio
import struct
from .domain import Clock, GattCharacteristic
from .heart_rate import BleHeartRateSource
from .protocol import BIKE, CONTROL, FEATURE, FTMS, HEART, HRS, POWER_RANGE, STATUS
from .trainer import FtmsTrainer
from .transport import Lost, Notify


class ManualClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.elapsed

    def utcnow(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed)

    def advance(self, seconds: float) -> None:
        import math
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("clock cannot move backwards or by a nonfinite amount")
        self.elapsed += seconds


@dataclass
class Faults:
    rejected_control: bool = False
    timeout: bool = False
    cadence_collapse: bool = False
    silent_hr: bool = False
    response_delay_sec: float = 0


class MemoryTransport:
    def __init__(self, role: str, on_disconnect: Lost, faults: Faults) -> None:
        self.role, self.on_disconnect, self.faults = role, on_disconnect, faults
        self.is_connected = False
        self.callbacks: dict[str, Notify] = {}
        self.writes: list[bytes] = []
        self.target_w: int | None = None
        self.running = False
        self.owned = False
        self.subscriptions_failed: set[str] = set()
        self.reads_failed: set[str] = set()
        self.characteristics = (
            (GattCharacteristic(FTMS, BIKE, ("notify",)),
             GattCharacteristic(FTMS, FEATURE, ("read",)),
             GattCharacteristic(FTMS, POWER_RANGE, ("read",)),
             GattCharacteristic(FTMS, CONTROL, ("write", "indicate")),
             GattCharacteristic(FTMS, STATUS, ("notify",)))
            if role == "trainer" else (GattCharacteristic(HRS, HEART, ("notify",)),))

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False
        self.callbacks.clear()
        self.owned = False

    def lose_connection(self) -> None:
        self.is_connected = False
        self.owned = False
        self.on_disconnect()

    async def read_gatt_char(self, characteristic: str) -> bytes:
        if characteristic in self.reads_failed:
            raise OSError("simulated read failure")
        return {FEATURE: struct.pack("<II", (1 << 14) | 2, 8),
                POWER_RANGE: struct.pack("<hhH", 0, 1000, 1)}[characteristic]

    async def subscribe(self, characteristic: str, callback: Notify) -> None:
        if characteristic in self.subscriptions_failed:
            raise OSError("simulated subscription failure")
        self.callbacks[characteristic] = callback

    def notify(self, characteristic: str, data: bytes) -> None:
        if self.is_connected and characteristic in self.callbacks:
            self.callbacks[characteristic](data)

    async def write_gatt_char(self, characteristic: str, data: bytes,
                              response: bool = True) -> None:
        if characteristic != CONTROL or not self.is_connected:
            raise ConnectionError("simulated transport unavailable")
        self.writes.append(data)
        if self.faults.timeout and data[0] != 8:
            return
        await asyncio.sleep(self.faults.response_delay_sec)
        if not self.is_connected:
            raise ConnectionError("simulated disconnect during procedure")
        opcode = data[0]
        result = 1
        if opcode == 0:
            if self.faults.rejected_control:
                result = 5
            else:
                self.owned = True
        elif not self.owned:
            result = 5
        elif opcode == 5:
            self.target_w = int.from_bytes(data[1:], "little", signed=True)
        elif opcode == 7:
            self.running = True
        elif opcode == 8:
            self.running = False
            if data[1] == 1:
                self.target_w = None
        else:
            result = 2
        self.notify(CONTROL, bytes([0x80, opcode, result]))


class SimulatedTrainer(FtmsTrainer):
    def __init__(self, *, clock: Clock | None = None, faults: Faults | None = None,
                 command_timeout_sec: float = 5, event_capacity: int = 256) -> None:
        self.faults = faults or Faults()
        self.transport: MemoryTransport | None = None

        def factory(on_disconnect: Lost) -> MemoryTransport:
            self.transport = MemoryTransport("trainer", on_disconnect, self.faults)
            return self.transport

        super().__init__(factory, clock=clock, command_timeout_sec=command_timeout_sec,
                         event_capacity=event_capacity)

    def emit_sample(self, *, power_w: int | None = None, cadence_rpm: float = 80) -> None:
        if self.transport is None:
            raise RuntimeError("connect simulator first")
        power = power_w if power_w is not None else (self.transport.target_w or 0)
        cadence = 0 if self.faults.cadence_collapse else round(cadence_rpm * 2)
        self.transport.notify(BIKE, struct.pack("<HHHh", 0x44, 2000, cadence, power))

    def lose_connection(self) -> None:
        if self.transport:
            self.transport.lose_connection()


class SimulatedHeartRateSource(BleHeartRateSource):
    def __init__(self, *, clock: Clock | None = None, faults: Faults | None = None,
                 stale_sec: float = 5, lost_sec: float = 15, event_capacity: int = 256) -> None:
        self.faults = faults or Faults()
        self.transport: MemoryTransport | None = None

        def factory(on_disconnect: Lost) -> MemoryTransport:
            self.transport = MemoryTransport("hr", on_disconnect, self.faults)
            return self.transport

        super().__init__(factory, clock=clock, stale_sec=stale_sec, lost_sec=lost_sec,
                         event_capacity=event_capacity)

    def emit_sample(self, bpm: int = 120) -> None:
        if self.transport is None:
            raise RuntimeError("connect simulator first")
        if not self.faults.silent_hr:
            self.transport.notify(HEART, struct.pack("<BH", 1, bpm))

    def lose_connection(self) -> None:
        if self.transport:
            self.transport.lose_connection()
