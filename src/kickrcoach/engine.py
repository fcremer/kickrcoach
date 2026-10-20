"""Monotonic, tick-driven M2 ERG engine; independent of BLE and web frameworks."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import AsyncIterator

from .domain import (Clock, ConnectionState as CS, ControlState as TS, Freshness, HeartRateSource,
                     StateEvent, SystemClock, TrainerAdapter, TrainerSample)
from .safety import SafetyController
from .workout import Workout
from .workout_safety import WorkoutGuard, WorkoutSafetyConfig

log = logging.getLogger(__name__)


class WorkoutState(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class WorkoutStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkoutEvent:
    monotonic: float
    state: WorkoutState
    reason: str
    elapsed_sec: float
    block_index: int
    target_w: int | None


class WorkoutEngine:
    """Call tick regularly (runner: 4 Hz). One consumer owns each adapter queue.

    Public operations serialize; STOP invalidates queued operations before waiting
    and cancels active work. This class never writes a GATT characteristic.
    """
    def __init__(self, trainer: TrainerAdapter, hr: HeartRateSource, workout: Workout, *,
                 baseline_w: int | None = None, safety: SafetyController | None = None,
                 config: WorkoutSafetyConfig | None = None, clock: Clock | None = None):
        self.trainer, self.hr, self.workout = trainer, hr, workout
        self.baseline_w = baseline_w
        self.safety = safety or SafetyController()
        self.config = config or WorkoutSafetyConfig()
        self.guard = WorkoutGuard(self.config)
        self.clock = clock or SystemClock()
        self.state = WorkoutState.IDLE
        self.block_index = 0
        self.targets: tuple[int, ...] = ()
        self.override_w: int | None = None
        self.sample: TrainerSample | None = None
        self.recovery_required = False
        self.recovery_armed = False
        self._elapsed = 0.0
        self._since: float | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._active: asyncio.Task[None] | None = None
        self._operation_done: asyncio.Event | None = None
        self._next_reconnect = 0.0
        self._next_hr_reconnect = 0.0
        self._hr_reconnect_task: asyncio.Task[None] | None = None
        self.events: asyncio.Queue[WorkoutEvent] = asyncio.Queue(maxsize=256)
        self.dropped_events = 0

    @property
    def elapsed_sec(self) -> float:
        return min(self.workout.duration_sec, self._elapsed +
                   (max(0, self.clock.monotonic() - self._since) if self._since is not None else 0))

    @property
    def remaining_sec(self) -> float:
        return max(0, self.workout.duration_sec - self.elapsed_sec)

    @property
    def planned_target_w(self) -> int | None:
        return self.targets[self.block_index] if self.targets else None

    def _event(self, reason: str) -> None:
        event = WorkoutEvent(self.clock.monotonic(), self.state, reason, self.elapsed_sec,
                             self.block_index, self.trainer.target_power_w)
        if self.events.full():
            self.events.get_nowait()
            self.events.task_done()
            self.dropped_events += 1
        self.events.put_nowait(event)
        log.info("workout state=%s reason=%s block=%s elapsed=%.3f target=%s",
                 event.state, reason, event.block_index, event.elapsed_sec, event.target_w)

    def _freeze(self) -> None:
        self._elapsed = self.elapsed_sec
        self._since = None

    def _require(self, *states: WorkoutState) -> None:
        if self.state not in states:
            raise WorkoutStateError(f"operation unavailable while {self.state}")

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                raise WorkoutStateError("operation invalidated by STOP")
            self._active = asyncio.current_task()
            done = asyncio.Event()
            self._operation_done = done
            try:
                yield
            except (Exception, asyncio.CancelledError) as exc:
                if not isinstance(exc, WorkoutStateError) and self.state not in (
                        WorkoutState.ABORTED, WorkoutState.COMPLETED):
                    self._freeze()
                    self.state = (WorkoutState.ABORTED if self.state == WorkoutState.PREPARING
                                  or self._elapsed >= self.workout.duration_sec else WorkoutState.PAUSED)
                    self.recovery_required = self.state == WorkoutState.PAUSED
                    self.recovery_armed = False
                    self._event(f"operation_failed:{type(exc).__name__}")
                    try:
                        await self.trainer.stop()
                    except Exception:
                        self._event("stop_unconfirmed:physical_stop_may_be_required")
                raise
            finally:
                self._active = None
                done.set()

    def _drain(self) -> None:
        while not self.trainer.events.empty():
            event = self.trainer.events.get_nowait()
            if isinstance(event, TrainerSample):
                self.sample = event
            elif isinstance(event, StateEvent) and event.connection in (CS.DISCONNECTED, CS.DEGRADED):
                self.sample = None
            self.trainer.events.task_done()
        # HR freshness remains authoritative on hr.latest, including when silent.
        self.hr.refresh()
        while not self.hr.events.empty():
            self.hr.events.get_nowait()
            self.hr.events.task_done()

    async def _target(self, watts: int, reason: str) -> None:
        self._require(WorkoutState.READY, WorkoutState.RUNNING, WorkoutState.PAUSED)
        caps = await self.trainer.capabilities()
        self.safety.validate(watts, caps.power_range)
        await self.trainer.set_target_power(watts)
        self._event(f"target_confirmed:{reason}:{watts}")

    async def prepare(self) -> None:
        async with self._operation():
            self._require(WorkoutState.IDLE)
            self.state = WorkoutState.PREPARING
            self._event("preparing")
            # Resolve the entire immutable definition before connecting or controlling.
            self.targets = tuple(block.target.resolve(self.baseline_w) for block in self.workout.blocks)
            results = await asyncio.gather(self.trainer.connect(), self.hr.connect(), return_exceptions=True)
            if isinstance(results[0], BaseException):
                raise ConnectionError("trainer setup failed")
            if isinstance(results[1], BaseException):
                self._event("hr_unavailable:static_workout_allowed")
            caps = await self.trainer.capabilities()
            if not caps.can_set_power:
                raise ValueError("confirmed target-power support and range required")
            for target in self.targets:
                self.safety.validate(target, caps.power_range)
            self.state = WorkoutState.READY
            self._drain()
            self._event("ready:no_control_requested")

    def _require_live_cadence(self) -> None:
        self._drain()
        if not self.guard.can_resume(self.sample, self.workout.blocks[self.block_index], self.clock.monotonic()):
            raise WorkoutStateError("fresh trainer power and safe positive cadence required")

    async def start(self) -> None:
        async with self._operation():
            self._require(WorkoutState.READY)
            self._require_live_cadence()
            await self.trainer.request_control()
            await self._target(self.targets[0], "start")
            await self.trainer.resume()
            self.guard.reset()
            self._since = self.clock.monotonic()
            self.state = WorkoutState.RUNNING
            self._event("started")

    async def _suspend(self, reason: str, *, recover: bool = False) -> None:
        self._freeze()
        self.state = WorkoutState.PAUSED
        self.recovery_required = recover
        self.recovery_armed = False
        self.guard.reset()
        self._event(reason)
        if recover:
            # Stop is best effort; transport loss cannot be reported as confirmed Stop.
            try:
                await self.trainer.stop()
            except Exception:
                self._event("stop_unconfirmed:physical_stop_may_be_required")
        else:
            await self.trainer.pause()

    async def pause(self) -> None:
        async with self._operation():
            self._require(WorkoutState.RUNNING)
            await self._suspend("manual_pause")

    async def resume(self) -> None:
        async with self._operation():
            self._require(WorkoutState.PAUSED)
            if self.remaining_sec <= 0:
                raise WorkoutStateError("workout has no remaining time; STOP required")
            if self.recovery_required and not self.recovery_armed:
                raise WorkoutStateError("safe recovery has not completed")
            self._require_live_cadence()
            if self.trainer.control_state != TS.PAUSED and not self.recovery_armed:
                raise WorkoutStateError("trainer must be recovered before resume")
            await self.trainer.resume()
            self.recovery_required = self.recovery_armed = False
            self.guard.reset()
            self.state = WorkoutState.RUNNING
            self._since = self.clock.monotonic()
            self._event("explicit_resume")

    async def _advance(self) -> bool:
        self._drain()
        if self.trainer.connection != CS.CONNECTED or self.trainer.control_state != TS.RUNNING:
            await self._suspend("trainer_control_lost", recover=True)
            return False
        reason = self.guard.reason(self.sample, self.workout.blocks[self.block_index], self.clock.monotonic())
        if reason:
            await self._suspend(f"safety:{reason}")
            return False
        if self.remaining_sec <= 0:
            self._freeze()
            await self.trainer.stop()
            self.state = WorkoutState.COMPLETED
            self._event("completed")
            return False
        elapsed = self.elapsed_sec
        boundary = 0
        for index, block in enumerate(self.workout.blocks):
            boundary += block.duration_sec
            if elapsed < boundary:
                if index != self.block_index:
                    self.block_index = index
                    self.override_w = None
                    self.guard.reset()
                    await self._target(self.targets[index], "block_transition")
                    self._event("block_changed:override_cleared")
                break
        return True

    async def adjust(self, delta_w: int) -> None:
        if type(delta_w) is not int or delta_w not in (-5, 5):
            raise ValueError("manual adjustment must be -5 or +5 W")
        async with self._operation():
            self._require(WorkoutState.RUNNING)
            if not await self._advance():
                raise WorkoutStateError("adjustment blocked by workout/safety state")
            current = self.trainer.target_power_w
            if current is None:
                raise WorkoutStateError("no acknowledged target")
            # Validate before mutating the override. Invalid manual input is refused
            # without stopping an otherwise healthy workout.
            target = current + delta_w
            caps = await self.trainer.capabilities()
            try:
                self.safety.validate(target, caps.power_range)
            except ValueError as exc:
                self._event("manual_override_rejected:bounds")
                raise WorkoutStateError(str(exc)) from exc
            await self._target(target, "manual_override_current_block")
            self.override_w = target

    async def _recover(self) -> None:
        now = self.clock.monotonic()
        if now < self._next_reconnect:
            return
        self._next_reconnect = now + self.config.reconnect_interval_sec
        self._event("recovery_attempt")
        try:
            await self.trainer.disconnect()
            await self.trainer.connect()
            self.sample = None
            self._drain()
            await self.trainer.request_control()
            # Never restore the previous target; round DOWN to the advertised step.
            caps = await self.trainer.capabilities()
            supported = caps.power_range
            if supported is None:
                raise ValueError("recovery range unknown")
            target = min(self.config.recovery_target_w, self.targets[self.block_index])
            target -= (target - supported.minimum) % supported.increment
            await self._target(target, "safe_recovery")
            self.override_w = target
            self.recovery_armed = True
            self._event("recovery_ready:explicit_resume_required")
        except Exception as exc:
            self.recovery_armed = False
            self._event(f"recovery_failed:{type(exc).__name__}")
            try:
                await self.trainer.disconnect()
            except Exception:
                self._event("recovery_cleanup_unconfirmed")

    async def tick(self) -> None:
        async with self._operation():
            self._require(WorkoutState.READY, WorkoutState.RUNNING, WorkoutState.PAUSED)
            self._drain()
            if self.state == WorkoutState.RUNNING:
                await self._advance()
            elif self.state == WorkoutState.PAUSED:
                if self.remaining_sec <= 0:
                    await self.stop()
                    return
                if self.trainer.connection != CS.CONNECTED or self.trainer.control_state not in (TS.PAUSED, TS.READY):
                    self.recovery_required, self.recovery_armed = True, False
                if self.recovery_required and not self.recovery_armed:
                    await self._recover()
            # Reconnecting HR must never block trainer safety/scheduling ticks.
            now = self.clock.monotonic()
            if (self.state in (WorkoutState.READY, WorkoutState.RUNNING, WorkoutState.PAUSED)
                    and (self.hr.connection != CS.CONNECTED or self.hr.latest.freshness == Freshness.LOST)
                    and now >= self._next_hr_reconnect
                    and (self._hr_reconnect_task is None or self._hr_reconnect_task.done())):
                self._next_hr_reconnect = now + self.config.reconnect_interval_sec
                self._hr_reconnect_task = asyncio.create_task(self._reconnect_hr())

    async def _reconnect_hr(self) -> None:
        try:
            await self.hr.disconnect()
            await self.hr.connect()
            self._event("hr_reconnected:awaiting_fresh_measurement")
        except Exception:
            self._event("hr_reconnect_failed:static_workout_continues")

    async def close(self) -> None:
        """Runtime owner must close on shutdown, including cancellation/failure."""
        if self.state != WorkoutState.COMPLETED:
            await self.stop()
        if self._hr_reconnect_task:
            self._hr_reconnect_task.cancel()
            await asyncio.gather(self._hr_reconnect_task, return_exceptions=True)
        results = await asyncio.gather(self.trainer.disconnect(), self.hr.disconnect(),
                                       return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            self._event("disconnect_unconfirmed:retry_cleanup")

    async def stop(self) -> None:
        """Local abort is immediate, even if FTMS Stop fails or a target is pending."""
        async with self._stop_lock:
            self._generation += 1
            self._freeze()
            if self.state == WorkoutState.COMPLETED:
                return
            self.state = WorkoutState.ABORTED
            self.recovery_required = self.recovery_armed = False
            self.override_w = None
            self.guard.reset()
            self._event("manual_stop:commands_invalidated")
            active = self._active
            done = self._operation_done
            if active and active is not asyncio.current_task():
                active.cancel()
            try:
                await self.trainer.stop()
            except Exception:
                self._event("stop_unconfirmed:physical_stop_may_be_required")
            if active and active is not asyncio.current_task() and done:
                # Await the operation, not its caller's entire task: that caller
                # may run close() in finally, which needs this same stop lock.
                await done.wait()
