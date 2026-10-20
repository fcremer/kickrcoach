"""Shared telemetry snapshots and simulation collection; no web framework."""
import asyncio
from collections import deque
import logging
import math
from .domain import ConnectionState, HeartRateSource, SystemClock, TrainerAdapter, TrainerSample, StateEvent
from .simulation import SimulatedHeartRateSource, SimulatedTrainer
from .web_workout import WebWorkout

log = logging.getLogger(__name__)

class Telemetry:
    """One collector for all browsers; bounded ephemeral history, no recording."""
    def __init__(self, trainer: TrainerAdapter, hr: HeartRateSource, *, simulated: bool,
                 stale_sec: float = 5, lost_sec: float = 15, clock: SystemClock | None = None,
                 workout: WebWorkout | None = None):
        self.trainer, self.hr, self.simulated = trainer, hr, simulated
        self.clock = clock or SystemClock()
        self.stale_sec, self.lost_sec = stale_sec, lost_sec
        self.started = self.clock.monotonic()
        self.sample: TrainerSample | None = None
        self.history: deque[dict] = deque(maxlen=180)
        self.error: str | None = None
        self.sequence = 0
        self.workout = workout

    def snapshot(self) -> dict:
        now = self.clock.monotonic()
        age = None if self.sample is None else max(0, now - self.sample.received.monotonic)
        connected = self.trainer.connection == ConnectionState.CONNECTED
        freshness = ("LOST" if not connected or age is None or age >= self.lost_sec else
                     "STALE" if age >= self.stale_sec else "LIVE")
        live = self.sample if freshness == "LIVE" else None
        hr = self.hr.latest
        return {"simulated": self.simulated, "sequence": self.sequence,
                "elapsed_sec": max(0, now - self.started), "error": self.error,
                "workout": self.workout.snapshot() if self.workout else None,
                "trainer": {"connection": self.trainer.connection, "freshness": freshness,
                            "age_sec": age, "power_w": live.power_w if live else None,
                            "cadence_rpm": live.cadence_rpm if live else None},
                "hr": {"connection": self.hr.connection, "freshness": hr.freshness,
                       "bpm": hr.bpm,
                       "age_sec": max(0, now - hr.received.monotonic) if hr.received else None}}

    async def consume(self, source: TrainerAdapter | HeartRateSource) -> None:
        # One consumer per adapter, independent of how many browsers subscribe.
        while True:
            event = await source.events.get()
            if isinstance(event, TrainerSample):
                self.sample = event
            elif (source is self.trainer and isinstance(event, StateEvent)
                  and event.connection != ConnectionState.CONNECTED):
                self.sample = None
            source.events.task_done()

    async def collect(self) -> None:
        if self.workout is not None:
            try:
                await self.workout.prepare()
                ticks = 0
                while True:
                    await self.workout.step()
                    self.sample = self.workout.sample
                    if ticks % 4 == 0:
                        self.sequence += 1
                        point = self.snapshot()
                        self.history.append({"t":point["elapsed_sec"], "power":point["trainer"]["power_w"],
                                             "cadence":point["trainer"]["cadence_rpm"], "hr":point["hr"]["bpm"]})
                    ticks += 1
                    await asyncio.sleep(0.25)
            finally:
                await self.workout.close()
            return
        consumers = [asyncio.create_task(self.consume(source)) for source in (self.trainer, self.hr)]
        try:
            results = await asyncio.gather(self.trainer.connect(), self.hr.connect(),
                                           return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                self.error = "Geräteverbindung fehlgeschlagen. Bluetooth, Broadcast und Geräteauswahl prüfen; App neu starten."
                log.error("Telemetry device setup failed (details omitted to protect device identifiers)")
            step = 0
            while True:
                if self.simulated:
                    assert isinstance(self.trainer, SimulatedTrainer)
                    assert isinstance(self.hr, SimulatedHeartRateSource)
                    self.trainer.emit_sample(power_w=round(125 + 18 * math.sin(step / 9)),
                                             cadence_rpm=82 + 4 * math.sin(step / 7))
                    self.hr.emit_sample(round(118 + 6 * math.sin(step / 15)))
                await asyncio.sleep(1)
                self.sequence += 1
                point = self.snapshot()
                self.history.append({"t": point["elapsed_sec"],
                                     "power": point["trainer"]["power_w"],
                                     "cadence": point["trainer"]["cadence_rpm"],
                                     "hr": point["hr"]["bpm"]})
                step += 1
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            results = await asyncio.gather(self.trainer.disconnect(), self.hr.disconnect(),
                                           return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                log.error("Device cleanup unconfirmed; check Bluetooth connections")

