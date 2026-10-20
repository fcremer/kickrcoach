"""Receive-time HR freshness. No direct Apple Watch/HealthKit integration."""
from dataclasses import dataclass


@dataclass
class HeartRateState:
    stale_sec: float = 5
    lost_sec: float = 15
    last_received: float | None = None
    bpm: int | None = None
    connected: bool = False

    def update(self, bpm: int, received: float, contact: bool | None = None) -> None:
        if contact is False:
            self.bpm, self.last_received = None, None
        else:
            self.bpm, self.last_received = bpm, received

    def snapshot(self, now: float) -> dict[str, object]:
        age = None if self.last_received is None else now - self.last_received
        state = ("LOST" if not self.connected or age is None or age >= self.lost_sec
                 else "STALE" if age >= self.stale_sec else "LIVE")
        return {"state": state, "bpm": self.bpm if state == "LIVE" else None,
                "receive_age_sec": age}
