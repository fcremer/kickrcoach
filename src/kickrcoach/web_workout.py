"""Single workout owner for simulation or explicitly enabled hardware control."""
import asyncio
import logging
from collections.abc import Coroutine

from .domain import SystemClock, TrainerSample, TrainerAdapter, HeartRateSource, ConnectionState as CS, ControlState as TS, StateEvent
from .engine import WorkoutEngine, WorkoutState, WorkoutStateError
from .simulation import SimulatedTrainer, SimulatedHeartRateSource
from .workout import Workout
from .safety import SafetyController


log = logging.getLogger(__name__)

def demo_workouts() -> dict[str, Workout]:
    def make(name: str, blocks: list[tuple[str, int, int]]) -> Workout:
        return Workout.model_validate({'name':name, 'blocks':[
            {'name':label,'duration_sec':duration,'mode':'erg','target':{'watts':watts}}
            for label,duration,watts in blocks]})
    return {
        'baseline': make('Demo · 4 Minuten', [('Einrollen',60,40),('Gleichmäßig',120,70),('Ausrollen',60,40)]),
        'easy': make('Demo · 2 Minuten locker', [('Locker',120,50)]),
    }


class WebWorkout:
    """Owns both event queues. Telemetry broadcast reads this owner's snapshot."""
    def __init__(self, *, trainer: TrainerAdapter | None = None, hr: HeartRateSource | None = None,
                 hardware: bool = False, maximum_w: int = 300) -> None:
        self.clock = SystemClock()
        if hardware and (trainer is None or hr is None):
            raise ValueError("hardware sessions require real adapter instances")
        self.hardware = hardware
        self.safety = SafetyController(maximum_w)
        self.trainer = trainer if trainer is not None else SimulatedTrainer(clock=self.clock,safety=self.safety)
        self.hr = hr if hr is not None else SimulatedHeartRateSource(clock=self.clock)
        self.workouts = demo_workouts()
        if hardware:
            self.workouts = {key: value.model_copy(update={'name':value.name.replace('Demo · ', '')})
                             for key,value in self.workouts.items()}
        self.notice: str | None = None
        self.selected = 'baseline'
        self.engine = self._engine()
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.sample: TrainerSample | None = None
        self.generation = 0

    def _engine(self) -> WorkoutEngine:
        return WorkoutEngine(self.trainer, self.hr, self.workouts[self.selected], clock=self.clock,
                             safety=self.safety, manage_hr_connection=not self.hardware)

    async def _call(self, operation: Coroutine[object, object, None]) -> None:
        # The engine may cancel an active operation on STOP. Isolate that task
        # from the lifelong telemetry collector / HTTP request owner.
        task = asyncio.create_task(operation)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current and current.cancelling():
                raise
            raise WorkoutStateError("Befehl durch STOP verworfen.") from None

    async def prepare(self) -> None:
        await self._call(self.engine.prepare(connect=not self.hardware))
        if not self.hardware:
            self.trainer.emit_sample()
            self.hr.emit_sample()
        await self._call(self.engine.tick())
        self.ready.set()

    async def step(self) -> None:
        async with self.lock:
            if not self.hardware:
                self.trainer.emit_sample()
                self.hr.emit_sample()
            elif self.engine.state == WorkoutState.IDLE and self.trainer.connection == CS.CONNECTED:
                try:
                    await self.prepare()
                except (ValueError, RuntimeError) as exc:
                    if not self.notice or 'STOP wurde nicht bestätigt' not in self.notice:
                        self.notice = f"Workout nicht bereit: {exc}"
                    self.ready.set()
            elif self.engine.state == WorkoutState.READY and self.trainer.connection != CS.CONNECTED:
                await self.engine.stop()
                self.engine = self._engine()
                self.ready.clear()
            if self.engine.state in (WorkoutState.READY, WorkoutState.WAITING, WorkoutState.RUNNING, WorkoutState.PAUSED):
                try:
                    await self._call(self.engine.tick())
                except (RuntimeError, ValueError, TimeoutError):
                    if self.engine.state not in (WorkoutState.ABORTED, WorkoutState.PAUSED):
                        raise
                    if not self.notice or 'STOP wurde nicht bestätigt' not in self.notice:
                        self.notice = 'Workout angehalten. Verbindung prüfen; anschließend ausdrücklich fortsetzen.'
                self.sample = self.engine.sample
            else:
                while not self.trainer.events.empty():
                    event = self.trainer.events.get_nowait()
                    if isinstance(event, TrainerSample):
                        self.sample = event
                    elif isinstance(event, StateEvent) and event.connection != CS.CONNECTED:
                        self.sample = None
                    self.trainer.events.task_done()
                self.hr.refresh()
                while not self.hr.events.empty():
                    self.hr.events.get_nowait()
                    self.hr.events.task_done()

    def snapshot(self) -> dict:
        engine = self.engine
        current = engine.workout.blocks[engine.block_index]
        next_block = (engine.workout.blocks[engine.block_index+1].name
                      if engine.block_index+1 < len(engine.workout.blocks) else None)
        # Drain diagnostics here; no persistence is claimed for this UI preview.
        while not engine.events.empty():
            event = engine.events.get_nowait()
            if 'stop_unconfirmed' in event.reason or 'physical_stop' in event.reason:
                message = 'STOP wurde nicht bestätigt. Treten beenden und Trainer entlasten.'
                if self.notice != message:
                    log.error(message)
                self.notice = message
            engine.events.task_done()
        return {'state':engine.state, 'mode':'hardware' if self.hardware else 'simulation',
                'notice':self.notice, 'maximum_w':self.safety.maximum_w,
                'can_start':engine.state == WorkoutState.READY and self.trainer.connection == CS.CONNECTED,
                'can_resume':engine.state == WorkoutState.PAUSED and self.trainer.connection == CS.CONNECTED and
                             (not engine.recovery_required or engine.recovery_armed), 'elapsed_sec':engine.elapsed_sec,
                'remaining_sec':engine.remaining_sec, 'block':current.name, 'next_block':next_block,
                'planned_target_w':engine.planned_target_w, 'target_w':self.trainer.target_power_w,
                'override_w':engine.override_w, 'recovery_armed':engine.recovery_armed,
                'selected':self.selected,
                'choices':[{'id':key,'name':value.name} for key,value in self.workouts.items()]}

    async def command(self, action: str, selected: str | None = None) -> None:
        generation = self.generation
        if action == 'stop':
            self.generation += 1
            # STOP bypasses the session lock and lets the engine preempt active work.
            await self.engine.stop()
            self.ready.set()
            return
        if not self.ready.is_set():
            raise ValueError('Warte auf Trainer und Workout-Vorbereitung.')
        async with self.lock:
            if generation != self.generation:
                raise ValueError("Befehl durch STOP verworfen.")
            engine = self.engine
            if action == 'select':
                if selected not in self.workouts:
                    raise ValueError('Unbekanntes Workout.')
                if engine.state not in (WorkoutState.READY, WorkoutState.ABORTED, WorkoutState.COMPLETED):
                    raise ValueError('Workout zuerst stoppen, bevor du ein anderes auswählst.')
                if self.hardware:
                    if self.trainer.connection != CS.CONNECTED or self.trainer.control_state != TS.READONLY:
                        raise ValueError('Warte auf eine bestätigte neue Trainerverbindung vor der Workout-Auswahl.')
                    if engine.state != WorkoutState.COMPLETED:
                        await engine.stop()
                else:
                    await engine.close()
                if not self.notice or 'STOP wurde nicht bestätigt' not in self.notice:
                    self.notice = None
                self.selected = selected
                self.engine = self._engine()
                await self.prepare()
                if generation != self.generation:
                    await self.engine.stop()
            elif action == 'start':
                if self.hardware and (not engine.targets or engine.targets[0] > min(50,self.safety.maximum_w)):
                    raise ValueError('Die Startleistung muss höchstens 50 W betragen.')
                await self._call(engine.start(wait_for_pedaling=True))
            elif action == 'pause':
                await self._call(engine.pause())
            elif action == 'resume':
                await self._call(engine.resume())
            elif action in ('increase','decrease'):
                await self._call(engine.adjust(5 if action == 'increase' else -5))
            else:
                raise ValueError('Unbekannter Befehl.')

    async def close(self) -> None:
        await self.engine.close()
        self.snapshot()  # Surface/log an unconfirmed final Stop during shutdown too.
