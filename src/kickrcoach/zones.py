"""User-defined HR zone boundaries; no estimated thresholds or control policy."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HeartRateZones:
    # Inclusive first BPM of zones 2, 3, 4, 5. Empty means unconfigured.
    boundaries: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if len(self.boundaries) not in (0, 4) or any(
                type(value) is not int or not 1 <= value <= 65535 or
                (index > 0 and value <= self.boundaries[index-1])
                for index, value in enumerate(self.boundaries)):
            raise ValueError("hr_zone_boundaries requires four increasing positive integer BPM values, or []")
