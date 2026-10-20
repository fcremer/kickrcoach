"""Generic BLE HRS adapter; HeartCast and chest straps use the same contract."""
import asyncio
import math
from .domain import (Clock, ConnectionState as CS, Freshness, HeartRateSample,
                     ReceivedAt, StateEvent)
from .events import EventSource
from .protocol import HEART, HRS, PacketError, heart_rate
from .sensors import HeartRateState
from .transport import GattTransport, TransportFactory


class BleHeartRateSource(EventSource):
    def __init__(self, factory: TransportFactory, *, clock: Clock | None = None,
                 stale_sec: float = 5, lost_sec: float = 15,
                 io_timeout_sec: float = 10, event_capacity: int = 256) -> None:
        super().__init__(clock, event_capacity)
        if (not all(math.isfinite(n) for n in (stale_sec, lost_sec, io_timeout_sec))
                or not 0 < stale_sec < lost_sec or not 0 < io_timeout_sec <= 30):
            raise ValueError("require 0 < stale_sec < lost_sec and 0 < io_timeout_sec <=30")
        self.factory = factory
        self.io_timeout_sec = io_timeout_sec
        self.connection = CS.DISCONNECTED
        self._hr = HeartRateState(stale_sec, lost_sec)
        self._transport: GattTransport | None = None
        self._watcher: asyncio.Task[None] | None = None
        self._lifecycle = asyncio.Lock()
        self._session = 0
        self._closing = False
        self._received: ReceivedAt | None = None
        self._contact: bool | None = None
        self._rr: tuple[float, ...] = ()
        self._emitted: Freshness | None = None

    def _state(self, state: CS, reason: str) -> None:
        self.connection = state
        self.publish(StateEvent(ReceivedAt.now(self.clock), state, None, reason))
        self.diagnostic("hr_state", f"{state}: {reason}")

    @property
    def latest(self) -> HeartRateSample:
        now = ReceivedAt.now(self.clock)
        snapshot = self._hr.snapshot(now.monotonic)
        freshness = Freshness(str(snapshot["state"]))
        return HeartRateSample(now, self._received,
                               self._hr.bpm if freshness == Freshness.LIVE else None,
                               freshness, self._contact,
                               self._rr if freshness == Freshness.LIVE else ())

    def refresh(self) -> HeartRateSample:
        latest = self.latest
        if latest.freshness != self._emitted:
            self.publish(latest)
            self._emitted = latest.freshness
        return latest

    def _lost(self, session: int) -> None:
        if session != self._session or self._closing:
            return
        self._hr.connected = False
        self._state(CS.DEGRADED, "HR connection lost; trainer control remains independent")
        self._state(CS.DISCONNECTED, "explicit reconnect required")
        self.refresh()
        if self._watcher:
            self._watcher.cancel()

    def _receive(self, session: int, data: bytes) -> None:
        if session != self._session or self._closing or not self._hr.connected:
            return
        try:
            decoded = heart_rate(data)
        except PacketError as exc:
            self.diagnostic("invalid_hr_packet", str(exc))
            return  # Invalid notifications do not reset freshness.
        now = ReceivedAt.now(self.clock)
        self._contact = decoded["contact"]
        self._rr = tuple(decoded["rr_sec"])
        self._hr.update(int(decoded["bpm"]), now.monotonic, self._contact)
        self._received = now if self._contact is not False else None
        self._emitted = self.latest.freshness
        self.publish(self.latest)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(min(0.5, self._hr.stale_sec / 2))
            self.refresh()

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.connection == CS.CONNECTED:
                return
            if self._transport is not None:
                raise RuntimeError("disconnect old HR session before reconnecting")
            self._session += 1
            session = self._session
            self._closing = False
            self._hr.last_received, self._hr.bpm = None, None
            self._received, self._contact, self._rr = None, None, ()
            self._emitted = None
            self._state(CS.CONNECTING, "connect requested")
            try:
                self._transport = self.factory(lambda: self._lost(session))
                async with asyncio.timeout(self.io_timeout_sec):
                    await self._transport.connect()
                    available = any(c.service == HRS and c.uuid == HEART and "notify" in c.properties
                                    for c in self._transport.characteristics)
                    if not available:
                        raise RuntimeError("standard HRS measurement notifications unavailable")
                    self._hr.connected = True
                    await self._transport.subscribe(HEART, lambda data: self._receive(session, data))
                    if not self._transport.is_connected or self.connection != CS.CONNECTING:
                        raise ConnectionError("HR lost during setup")
                self._state(CS.CONNECTED, "HRS notifications ready")
                self.refresh()
                self._watcher = asyncio.create_task(self._watch())
            except BaseException:
                self._state(CS.DEGRADED, "HR setup failed")
                await self._close()
                raise

    async def _close(self) -> None:
        self._closing = True
        self._session += 1
        self._hr.connected = False
        if self._watcher:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
        transport = self._transport
        try:
            if transport:
                async with asyncio.timeout(self.io_timeout_sec):
                    await transport.disconnect()
        except BaseException:
            self._state(CS.DEGRADED, "HR disconnect unconfirmed; retry cleanup")
            self.refresh()
            raise
        else:
            self._transport = None
            self._state(CS.DISCONNECTED, "HR session closed")
            self.refresh()

    async def disconnect(self) -> None:
        async with self._lifecycle:
            await self._close()
