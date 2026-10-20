"""M0 manual-test bounds; no training policy or HR-based control."""
from dataclasses import dataclass
from .protocol import PowerRange


@dataclass(frozen=True)
class SafetyController:
    maximum_w: int = 100

    def validate(self, watts: int, supported: PowerRange | None) -> int:
        if type(watts) is not int or not 1 <= watts <= self.maximum_w <= 100:
            raise ValueError("M0 target must be an integer within 1..configured max (<=100 W)")
        if supported is None:
            raise ValueError("power range unknown: refusing manual target test")
        if not supported.minimum <= watts <= supported.maximum:
            raise ValueError("target outside discovered power range")
        if (watts - supported.minimum) % supported.increment:
            raise ValueError("target not aligned with discovered power increment")
        return watts
