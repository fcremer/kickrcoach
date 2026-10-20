"""Validate requested targets against configured policy and discovered FTMS range."""
from dataclasses import dataclass
from .protocol import PowerRange


@dataclass(frozen=True)
class SafetyController:
    maximum_w: int = 100

    def __post_init__(self) -> None:
        if type(self.maximum_w) is not int or not 1 <= self.maximum_w <= 32767:
            raise ValueError("configured maximum must be an integer within 1..32767 W")

    def validate(self, watts: int, supported: PowerRange | None) -> int:
        if type(watts) is not int or not 1 <= watts <= self.maximum_w:
            raise ValueError(f"Zielleistung muss eine ganze Zahl zwischen 1 und {self.maximum_w} W sein.")
        if supported is None:
            raise ValueError("power range unknown: refusing target")
        if not supported.minimum <= watts <= supported.maximum:
            raise ValueError("target outside discovered power range")
        if (watts - supported.minimum) % supported.increment:
            raise ValueError("target not aligned with discovered power increment")
        return watts
