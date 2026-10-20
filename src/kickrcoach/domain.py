"""BLE-independent M1 contracts, immutable events and explicit adapter states."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import time
from typing import Protocol, runtime_checkable
from .protocol import PowerRange


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"


class ControlState(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    READONLY = "READONLY"
    ACQUIRING = "ACQUIRING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    UNCERTAIN = "UNCERTAIN"


class Freshness(StrEnum):
    LIVE = "LIVE"
    STALE = "STALE"
    LOST = "LOST"


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def utcnow(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ReceivedAt:
    utc: datetime
    monotonic: float
    source_utc: datetime | None = None

    @classmethod
    def now(cls, clock: Clock) -> "ReceivedAt":
        return cls(clock.utcnow(), clock.monotonic())


@dataclass(frozen=True)
class TrainerSample:
    received: ReceivedAt
    power_w: int | None = None
    cadence_rpm: float | None = None
    speed_kmh: float | None = None


@dataclass(frozen=True)
class HeartRateSample:
    observed: ReceivedAt
    received: ReceivedAt | None
    bpm: int | None
    freshness: Freshness
    contact: bool | None = None
    rr_sec: tuple[float, ...] = ()


@dataclass(frozen=True)
class StateEvent:
    observed: ReceivedAt
    connection: ConnectionState
    control: ControlState | None
    reason: str


@dataclass(frozen=True)
class DiagnosticEvent:
    observed: ReceivedAt
    kind: str
    detail: str


AdapterEvent = TrainerSample | HeartRateSample | StateEvent | DiagnosticEvent


@dataclass(frozen=True)
class GattCharacteristic:
    service: str
    uuid: str
    properties: tuple[str, ...]


@dataclass(frozen=True)
class TrainerCapabilities:
    characteristics: tuple[GattCharacteristic, ...] = ()
    feature_flags: int | None = None
    target_flags: int | None = None
    power_range: PowerRange | None = None
    can_control: bool = False
    can_set_power: bool = False
    status_subscribed: bool = False


@runtime_checkable
class TrainerAdapter(Protocol):
    connection: ConnectionState
    control_state: ControlState
    events: asyncio.Queue[AdapterEvent]
    target_power_w: int | None
    stop_confirmed: bool

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def capabilities(self) -> TrainerCapabilities: ...
    async def request_control(self) -> None: ...
    async def set_target_power(self, watts: int) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def stop(self) -> None: ...


@runtime_checkable
class HeartRateSource(Protocol):
    connection: ConnectionState
    events: asyncio.Queue[AdapterEvent]

    @property
    def latest(self) -> HeartRateSample: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def refresh(self) -> HeartRateSample: ...
