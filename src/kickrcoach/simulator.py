"""In-memory BLE-shaped M0 transport; never imports or opens Bluetooth."""
import asyncio
import struct
from collections.abc import Callable
from .protocol import BIKE, FEATURE, POWER_RANGE, CONTROL, STATUS, HEART

Callback = Callable[[object, bytearray], None]


class SimulatedClient:
    def __init__(self, role: str, scenario: str = "normal",
                 disconnected_callback: Callable[[object], None] | None = None) -> None:
        self.role, self.scenario = role, scenario
        self.is_connected = False
        self.callbacks: dict[str, Callback] = {}
        self.target = 50
        self._disconnected = disconnected_callback
        self._task: asyncio.Task[None] | None = None
        self.writes: list[bytes] = []
        self.services: list[object] = []

    async def connect(self) -> None:
        self.is_connected = True
        self._task = asyncio.create_task(self._feed())

    async def disconnect(self) -> None:
        self.is_connected = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def start_notify(self, characteristic: str, callback: Callback) -> None:
        self.callbacks[characteristic] = callback

    async def read_gatt_char(self, characteristic: str) -> bytearray:
        data = {FEATURE: struct.pack("<II", (1 << 14) | 2, 8),
                POWER_RANGE: struct.pack("<hhH", 0, 1000, 1)}
        return bytearray(data[characteristic])

    async def write_gatt_char(self, characteristic: str, data: bytes,
                              response: bool = True) -> None:
        assert characteristic == CONTROL
        self.writes.append(data)
        if self.scenario == "timeout" and data[0] != 8:
            return
        result = 5 if self.scenario == "rejected-control" and data[0] == 0 else 1
        if data[0] == 5 and result == 1:
            self.target = int.from_bytes(data[1:], "little", signed=True)
        self.callbacks[CONTROL](CONTROL, bytearray([0x80, data[0], result]))

    async def _feed(self) -> None:
        tick = 0
        while self.is_connected:
            await asyncio.sleep(1)
            tick += 1
            if self.scenario == "disconnect" and tick == 3 and self.role == "trainer":
                self.is_connected = False
                if self._disconnected:
                    self._disconnected(self)
                return
            if self.role == "trainer" and BIKE in self.callbacks:
                cadence = 0 if self.scenario == "cadence-collapse" and tick >= 3 else 160
                packet = struct.pack("<HHHh", 0x44, 2000, cadence, self.target)
                self.callbacks[BIKE](BIKE, bytearray(packet))
            if self.role == "hr" and HEART in self.callbacks:
                if self.scenario != "stale-hr" or tick <= 2:
                    self.callbacks[HEART](HEART, bytearray([0, 120]))
