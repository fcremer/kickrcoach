"""FTMS trainer adapter. No workout policy, Bleak import or implicit reconnect."""
import asyncio
from collections.abc import Callable, Awaitable
from dataclasses import replace
import math
from .control import TrainerControl
from .domain import (Clock, ConnectionState as CS, ControlState as TS, ReceivedAt,
                     StateEvent, TrainerCapabilities, TrainerSample)
from .events import EventSource
from .protocol import (BIKE, CONTROL, FEATURE, FTMS, POWER_RANGE, STATUS,
                       PacketError, PowerRange, features, indoor_bike)
from .safety import SafetyController
from .transport import GattTransport, TransportFactory


class AdapterStateError(RuntimeError):
    """Caller requested an operation which is invalid in the current state."""


class FtmsTrainer(EventSource):
    def __init__(self, factory: TransportFactory, *, safety: SafetyController | None = None,
                 clock: Clock | None = None, command_timeout_sec: float = 5,
                 io_timeout_sec: float = 10, event_capacity: int = 256) -> None:
        super().__init__(clock, event_capacity)
        if not all(math.isfinite(n) and 0 < n <= 30
                   for n in (command_timeout_sec, io_timeout_sec)):
            raise ValueError("timeouts must be finite, positive and <=30 seconds")
        self.factory = factory
        self.safety = safety or SafetyController()
        self.command_timeout_sec = command_timeout_sec
        self.io_timeout_sec = io_timeout_sec
        self.connection = CS.DISCONNECTED
        self.control_state = TS.UNAVAILABLE
        self.target_power_w: int | None = None
        self._caps = TrainerCapabilities()
        self._transport: GattTransport | None = None
        self._control: TrainerControl | None = None
        self._lifecycle = asyncio.Lock()
        self._commands = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._active: asyncio.Task[None] | None = None
        self._generation = 0
        self._session = 0
        self._closing = False
        self._target_confirmed = False
        self._expected_status: tuple[int, int] | None = None

    @property
    def stop_confirmed(self) -> bool:
        """True only after a successful FTMS stop result in this connection."""
        return self._control is not None and self._control.stop_confirmed

    def _state(self, connection: CS, control: TS, reason: str) -> None:
        self.connection, self.control_state = connection, control
        self.publish(StateEvent(ReceivedAt.now(self.clock), connection, control, reason))
        self.diagnostic("trainer_state", f"{connection}/{control}: {reason}")

    def _invalidate(self, reason: str) -> None:
        self._generation += 1
        self._expected_status = None
        self.target_power_w = None
        self._target_confirmed = False
        if self._control:
            self._control.invalidate(reason)

    def _lost(self, session: int) -> None:
        if session != self._session or self._closing:
            return
        self._invalidate("trainer disconnected")
        self._caps = TrainerCapabilities()
        self._state(CS.DEGRADED, TS.UNCERTAIN, "unexpected disconnect")
        self._state(CS.DISCONNECTED, TS.UNAVAILABLE, "control lost; explicit reconnect required")

    def _properties(self, characteristic: str) -> tuple[str, ...]:
        return next((c.properties for c in self._caps.characteristics
                     if c.service == FTMS and c.uuid == characteristic), ())

    def _valid_session(self, session: int) -> bool:
        return (session == self._session and not self._closing
                and self.connection in (CS.CONNECTING, CS.CONNECTED))

    async def connect(self) -> None:
        async with self._lifecycle:
            if self.connection == CS.CONNECTED:
                return
            if self.connection != CS.DISCONNECTED or self._transport is not None:
                raise AdapterStateError("disconnect old session before reconnecting")
            self._session += 1
            session = self._session
            self._closing = False
            self._state(CS.CONNECTING, TS.UNAVAILABLE, "connect requested")
            try:
                self._transport = self.factory(lambda: self._lost(session))
                async with asyncio.timeout(self.io_timeout_sec):
                    await self._transport.connect()
                    self._caps = TrainerCapabilities(characteristics=self._transport.characteristics)
                    if "notify" not in self._properties(BIKE):
                        raise AdapterStateError("FTMS Indoor Bike Data notifications unavailable")
                    machine, target, supported = await self._read_capabilities()
                    self._control = TrainerControl(self._transport, self.safety, supported,
                                                    self.command_timeout_sec)
                    await self._transport.subscribe(BIKE, lambda data: self._bike(session, data))
                    status_ok = await self._optional_subscribe(STATUS, "notify",
                                                              lambda data: self._status(session, data))
                    cp_ok = ("write" in self._properties(CONTROL)
                             and await self._optional_subscribe(CONTROL, "indicate",
                                                               lambda data: self._response(session, data)))
                    if not self._transport.is_connected or not self._valid_session(session):
                        raise ConnectionError("connection lost during setup")
                    self._caps = replace(self._caps, feature_flags=machine, target_flags=target,
                                         power_range=supported, can_control=bool(cp_ok),
                                         can_set_power=bool(cp_ok and target is not None and target & 8
                                                            and supported is not None),
                                         status_subscribed=status_ok)
                    self._state(CS.CONNECTED, TS.READONLY, "subscriptions ready; no control requested")
            except BaseException:
                self._invalidate("connection setup failed")
                self._state(CS.DEGRADED, TS.UNCERTAIN, "connection setup failed")
                await self._close_transport()
                raise

    async def _read_capabilities(self) -> tuple[int | None, int | None, PowerRange | None]:
        assert self._transport is not None
        machine = target = None
        supported = None
        for characteristic in (FEATURE, POWER_RANGE):
            if "read" not in self._properties(characteristic):
                self.diagnostic("capability_absent", characteristic)
                continue
            try:
                async with asyncio.timeout(self.io_timeout_sec / 4):
                    data = await self._transport.read_gatt_char(characteristic)
                if characteristic == FEATURE:
                    decoded = features(data)
                    machine, target = int(decoded["machine_bits"]), int(decoded["target_bits"])
                else:
                    supported = PowerRange.decode(data)
            except Exception as exc:
                self.diagnostic("capability_failed", f"{characteristic}: {type(exc).__name__}")
        return machine, target, supported

    async def _optional_subscribe(self, characteristic: str, prop: str,
                                  callback: Callable[[bytes], None]) -> bool:
        assert self._transport is not None
        if prop not in self._properties(characteristic):
            self.diagnostic("subscription_absent", characteristic)
            return False
        try:
            async with asyncio.timeout(self.io_timeout_sec / 8):
                await self._transport.subscribe(characteristic, callback)
            return True
        except Exception as exc:
            self.diagnostic("subscription_failed", f"{characteristic}: {type(exc).__name__}")
            return False

    def _bike(self, session: int, data: bytes) -> None:
        if not self._valid_session(session):
            return
        try:
            values = indoor_bike(data)
        except PacketError as exc:
            self.diagnostic("invalid_trainer_packet", str(exc))
            return
        self.publish(TrainerSample(ReceivedAt.now(self.clock),
                                   power_w=int(values["power_w"]) if "power_w" in values else None,
                                   cadence_rpm=float(values["cadence_rpm"]) if "cadence_rpm" in values else None,
                                   speed_kmh=float(values["speed_kmh"]) if "speed_kmh" in values else None))

    def _response(self, session: int, data: bytes) -> None:
        if (session == self._session and not self._closing and self._control
                and self.connection != CS.DISCONNECTED):
            self._control.indication(None, bytearray(data))

    def _status(self, session: int, data: bytes) -> None:
        if not self._valid_session(session):
            return
        self.diagnostic("ftms_status", data.hex())
        if not data:
            self.diagnostic("invalid_status", "empty FTMS status")
        elif data[0] in (1, 0xFF):
            self._invalidate("FTMS reset or control permission lost")
            self._state(CS.DEGRADED, TS.UNCERTAIN, "reset/control lost; reconnect required")
        elif data[0] == 2 and len(data) >= 2:
            if self._expected_status == (data[0], data[1]):
                self._expected_status = None
                return  # Status is advisory; the control-point indication is still required.
            self.target_power_w = None
            self._target_confirmed = False
            self._generation += 1
            # A remote stop/pause must not leave an in-flight target command valid.
            if self._active and self._control:
                self._control.invalidate("remote stop/pause during command")
                self._state(CS.DEGRADED, TS.UNCERTAIN, "remote stop during procedure")
            elif self.control_state in (TS.RUNNING, TS.PAUSED, TS.READY):
                self._state(CS.CONNECTED, TS.PAUSED if data[1] == 2 else TS.READY,
                            "remote stop/pause observed; target must be reconfirmed")

    async def capabilities(self) -> TrainerCapabilities:
        return self._caps

    def _require(self, allowed: tuple[TS, ...]) -> TrainerControl:
        if (self.connection != CS.CONNECTED or self.control_state not in allowed
                or self._control is None or self._transport is None
                or not self._transport.is_connected):
            raise AdapterStateError("operation unavailable in current connection/control state")
        return self._control

    async def _perform(self, action: Callable[[TrainerControl], Awaitable[None]],
                       allowed: tuple[TS, ...], result: TS | None = None,
                       acquiring: bool = False) -> None:
        generation = self._generation
        async with self._commands:
            if generation != self._generation:
                raise AdapterStateError("command invalidated by stop/disconnect")
            control = self._require(allowed)
            if acquiring:
                self._state(CS.CONNECTED, TS.ACQUIRING, "Request Control pending")
            self._active = asyncio.create_task(action(control))
            try:
                await self._active
                if generation != self._generation or self.connection != CS.CONNECTED:
                    raise AdapterStateError("command completed after state invalidation")
                if result is not None:
                    self._state(CS.CONNECTED, result, "FTMS procedure acknowledged")
            except BaseException:
                if generation == self._generation:
                    self._invalidate("command failed or cancelled")
                    self._state(CS.DEGRADED, TS.UNCERTAIN, "command failed; reconnect required")
                raise
            finally:
                self._active = None

    async def request_control(self) -> None:
        if not self._caps.can_control:
            raise AdapterStateError("control indications/write capability unavailable")
        await self._perform(lambda c: c.request_control(), (TS.READONLY,), TS.READY, acquiring=True)

    async def set_target_power(self, watts: int) -> None:
        if not self._caps.can_set_power:
            raise AdapterStateError("target-power feature or supported range unavailable")
        self.safety.validate(watts, self._caps.power_range)
        await self._perform(lambda c: c.set_target_power(watts), (TS.READY, TS.RUNNING, TS.PAUSED))
        self.target_power_w = watts
        self._target_confirmed = True

    async def pause(self) -> None:
        async def action(control: TrainerControl) -> None:
            self._expected_status = (2, 2)
            try:
                await control.pause()
            except BaseException:
                self._expected_status = None
                raise
        await self._perform(action, (TS.RUNNING,), TS.PAUSED)

    async def resume(self) -> None:
        if not self._target_confirmed:
            raise AdapterStateError("explicit acknowledged safe target required before start/resume")
        await self._perform(lambda c: c.start(), (TS.READY, TS.PAUSED), TS.RUNNING)

    async def stop(self) -> None:
        async with self._stop_lock:
            self._generation += 1
            self.target_power_w, self._target_confirmed = None, False
            if self.connection == CS.CONNECTED:
                self._state(CS.DEGRADED, TS.UNCERTAIN, "local stop requested; commands invalidated")
            if self._active:
                self._active.cancel()
                await asyncio.gather(self._active, return_exceptions=True)
            async with self._commands:
                if not self._control or not self._transport or not self._transport.is_connected:
                    return
                try:
                    await self._control.stop()
                    if self._control.uncertain:
                        self._state(CS.DEGRADED, TS.UNCERTAIN, "stopped; reconnect required")
                    else:
                        self._state(CS.CONNECTED, TS.READONLY, "stopped; explicit reacquisition required")
                except BaseException:
                    self._invalidate("stop unconfirmed")
                    if self.connection != CS.DISCONNECTED:
                        self._state(CS.DEGRADED, TS.UNCERTAIN, "physical stop may be required")
                    raise

    async def _close_transport(self) -> None:
        self._closing = True
        self._session += 1  # Ignore callbacks from a previous BLE session.
        transport = self._transport
        self._control = None
        self._caps = TrainerCapabilities()
        self._invalidate("session closed")
        try:
            if transport:
                async with asyncio.timeout(self.io_timeout_sec):
                    await transport.disconnect()
        except BaseException:
            # Retain the transport so an explicit disconnect can retry cleanup.
            self._state(CS.DEGRADED, TS.UNCERTAIN, "disconnect unconfirmed; retry cleanup")
            raise
        else:
            self._transport = None
            self._state(CS.DISCONNECTED, TS.UNAVAILABLE, "session closed")

    async def disconnect(self) -> None:
        async with self._lifecycle:
            try:
                await self.stop()
            except Exception as exc:
                self.diagnostic("stop_failed", f"{type(exc).__name__}: physical stop may be required")
            finally:
                await self._close_transport()
