"""Pure Bluetooth SIG FTMS / Heart Rate Measurement decoders."""
from dataclasses import dataclass
import struct
from typing import TypedDict


def uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


FTMS, HRS = uuid("1826"), uuid("180d")
BIKE, FEATURE, POWER_RANGE = uuid("2ad2"), uuid("2acc"), uuid("2ad8")
CONTROL, STATUS, HEART = uuid("2ad9"), uuid("2ada"), uuid("2a37")


class PacketError(ValueError):
    """Malformed, truncated or unsupported packet."""


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.offset = data, 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise PacketError(f"truncated packet at {self.offset}, need {size} bytes")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def number(self, size: int, signed: bool = False) -> int:
        return int.from_bytes(self.take(size), "little", signed=signed)


def indoor_bike(data: bytes) -> dict[str, float | int]:
    r = Reader(data)
    flags = r.number(2)
    if flags & 0xE000:
        raise PacketError("reserved Indoor Bike Data flags")
    result: dict[str, float | int] = {}
    # More Data bit means instantaneous speed is absent, not that all data is absent.
    if not flags & 1:
        result["speed_kmh"] = r.number(2) / 100
    if flags & (1 << 1):
        r.take(2)  # average speed
    if flags & (1 << 2):
        result["cadence_rpm"] = r.number(2) / 2
    if flags & (1 << 3):
        r.take(2)  # average cadence
    if flags & (1 << 4):
        r.take(3)  # distance
    if flags & (1 << 5):
        r.take(2)  # resistance
    if flags & (1 << 6):
        result["power_w"] = r.number(2, signed=True)
    # Validate every flagged field, even if not used by M0.
    for bit, size in ((7, 2), (8, 5), (9, 1), (10, 1), (11, 2), (12, 2)):
        if flags & (1 << bit):
            r.take(size)
    if r.offset != len(data):
        raise PacketError("unexpected Indoor Bike Data trailing bytes")
    return result


class HeartMeasurement(TypedDict):
    bpm: int
    contact: bool | None
    rr_sec: list[float]


def heart_rate(data: bytes) -> HeartMeasurement:
    r = Reader(data)
    flags = r.number(1)
    if flags & 0xE0:
        raise PacketError("reserved HR flags")
    bpm = r.number(2 if flags & 1 else 1)
    contact = bool(flags & 2) if flags & 4 else None
    if flags & 8:
        r.take(2)  # expended energy
    rr: list[float] = []
    if flags & 16:
        if r.offset == len(data):
            raise PacketError("RR flag without intervals")
        while r.offset < len(data):
            rr.append(r.number(2) / 1024)
    if r.offset != len(data) or bpm == 0:
        raise PacketError("invalid HR measurement")
    return {"bpm": bpm, "contact": contact, "rr_sec": rr}


@dataclass(frozen=True)
class PowerRange:
    minimum: int
    maximum: int
    increment: int

    @classmethod
    def decode(cls, data: bytes) -> "PowerRange":
        if len(data) != 6:
            raise PacketError("Supported Power Range must contain 6 bytes")
        low, high, step = struct.unpack("<hhH", data)
        if low > high or step == 0:
            raise PacketError("invalid Supported Power Range")
        return cls(low, high, step)


class FeatureFlags(TypedDict):
    machine_bits: int
    target_bits: int
    cadence_supported: bool
    power_supported: bool
    power_target_supported: bool


def features(data: bytes) -> FeatureFlags:
    if len(data) != 8:
        raise PacketError("Fitness Machine Feature must contain 8 bytes")
    machine, target = struct.unpack("<II", data)
    return {"machine_bits": machine, "target_bits": target,
            "cadence_supported": bool(machine & 2),
            "power_supported": bool(machine & (1 << 14)),
            "power_target_supported": bool(target & (1 << 3))}
