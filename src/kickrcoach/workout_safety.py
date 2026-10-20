"""Deterministic ERG guards; heart rate is observational, never a power target."""
from dataclasses import dataclass
import math
from .domain import TrainerSample
from .workout import Block


@dataclass(frozen=True)
class WorkoutSafetyConfig:
    telemetry_timeout_sec: float = 5
    cadence_collapse_sec: float = 3
    cadence_floor_rpm: float = 35
    anti_stall_rpm: float = 5
    recovery_target_w: int = 50
    reconnect_interval_sec: float = 5

    def __post_init__(self) -> None:
        values = (self.telemetry_timeout_sec, self.cadence_collapse_sec,
                  self.cadence_floor_rpm, self.anti_stall_rpm, self.reconnect_interval_sec)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("safety thresholds must be finite and positive")
        if self.anti_stall_rpm >= self.cadence_floor_rpm:
            raise ValueError("anti-stall threshold must be below ordinary cadence floor")
        if type(self.recovery_target_w) is not int or not 1 <= self.recovery_target_w <= 100:
            raise ValueError("recovery target must be within 1..100 W")


class WorkoutGuard:
    def __init__(self, config: WorkoutSafetyConfig):
        self.config = config
        self.low_since: float | None = None

    def reset(self) -> None:
        self.low_since = None

    def reason(self, sample: TrainerSample | None, block: Block, now: float) -> str | None:
        if (sample is None or now - sample.received.monotonic >= self.config.telemetry_timeout_sec
                or sample.power_w is None or sample.cadence_rpm is None):
            return "trainer_telemetry_unavailable"
        cadence = sample.cadence_rpm
        if not math.isfinite(cadence) or cadence < 0:
            return "trainer_telemetry_invalid"
        # Intentional low-cadence blocks get a lower collapse threshold. Anti-stall
        # remains active even with guidance; sustained zero cadence is never ignored.
        floor = self.config.cadence_floor_rpm
        if block.cadence:
            floor = min(floor, block.cadence.minimum_rpm * 0.5)
        floor = max(self.config.anti_stall_rpm, floor)
        if cadence < floor:
            if self.low_since is None:
                self.low_since = now
            if now - self.low_since >= self.config.cadence_collapse_sec:
                return "cadence_collapse"
        else:
            self.low_since = None
        return None

    def can_resume(self, sample: TrainerSample | None, block: Block, now: float) -> bool:
        if self.reason(sample, block, now) is not None or sample is None:
            return False
        floor = self.config.cadence_floor_rpm
        if block.cadence:
            floor = min(floor, block.cadence.minimum_rpm * 0.5)
        return sample.cadence_rpm is not None and sample.cadence_rpm >= max(self.config.anti_stall_rpm, floor)
