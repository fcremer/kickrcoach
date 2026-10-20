"""The sole FTMS writer. A write acknowledgement is NOT procedure success."""
import asyncio
import logging
import struct
from typing import Protocol
from .protocol import CONTROL, PowerRange
from .safety import SafetyController

log = logging.getLogger(__name__)


class Transport(Protocol):
    is_connected: bool

    async def write_gatt_char(self, characteristic: str, data: bytes,
                              response: bool = True) -> None: ...


class ControlError(RuntimeError):
    pass


class TrainerControl:
    def __init__(self, transport: Transport, safety: SafetyController,
                 supported: PowerRange | None, timeout: float = 5) -> None:
        self.transport, self.safety, self.supported = transport, safety, supported
        self.timeout = timeout
        self.owned = False
        self.attempted = False
        self.stop_confirmed = False
        self.uncertain = False
        self._lock = asyncio.Lock()
        self._pending: tuple[int, asyncio.Future[int]] | None = None
        self._poisoned: set[int] = set()

    def indication(self, _sender: object, data: bytearray) -> None:
        if len(data) != 3 or data[0] != 0x80:
            log.warning("malformed_control_response hex=%s", data.hex())
            return
        if self._pending and data[1] == self._pending[0]:
            future = self._pending[1]
            if not future.done():
                future.set_result(data[2])
        else:
            log.warning("unmatched_control_response hex=%s", data.hex())

    def invalidate(self, reason: str) -> None:
        self.owned, self.uncertain = False, True
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(ControlError(reason))

    async def _procedure(self, opcode: int, payload: bytes = b"") -> None:
        async with self._lock:
            if opcode in self._poisoned:
                raise ControlError("ambiguous previous procedure; reconnect before reusing opcode")
            if not self.transport.is_connected:
                raise ControlError("trainer disconnected")
            if self.uncertain and opcode != 0x08:
                raise ControlError("control uncertain; disconnect and explicitly restart")
            if opcode not in (0, 8) and not self.owned:
                raise ControlError("control not acquired")
            future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self._pending = opcode, future
            self.attempted = True
            if opcode != 8:
                self.stop_confirmed = False
            log.info("control_command opcode=%02x payload=%s", opcode, payload.hex())
            try:
                async with asyncio.timeout(self.timeout):
                    await self.transport.write_gatt_char(CONTROL, bytes([opcode]) + payload,
                                                         response=True)
                    result = await future
                log.info("control_result opcode=%02x result=%02x", opcode, result)
                if result != 1:
                    raise ControlError(f"FTMS opcode 0x{opcode:02x} rejected: 0x{result:02x}")
                if opcode == 0:
                    self.owned = True
            except BaseException:
                # FTMS has no transaction ID. A late Pause response (opcode 08)
                # must never be mistaken for acknowledgement of a later Stop.
                self._poisoned.add(opcode)
                self.owned, self.uncertain = False, True
                raise
            finally:
                self._pending = None
                if not future.done():
                    future.cancel()

    async def request_control(self) -> None:
        await self._procedure(0)

    async def set_target_power(self, watts: int) -> None:
        validated = self.safety.validate(watts, self.supported)
        await self._procedure(5, struct.pack("<h", validated))

    async def start(self) -> None:
        await self._procedure(7)

    async def pause(self) -> None:
        await self._procedure(8, b"\x02")

    async def stop(self) -> None:
        # Best effort even after uncertain ownership: result must still be checked.
        if self.attempted and not self.stop_confirmed and self.transport.is_connected:
            await self._procedure(8, b"\x01")
            self.stop_confirmed = True
        self.owned = False
