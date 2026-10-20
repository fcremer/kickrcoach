"""Single simulator session owner for the local workout preview; no hardware path."""
import asyncio
from collections.abc import Coroutine

from .domain import SystemClock, TrainerSample
from .engine import WorkoutEngine, WorkoutState, WorkoutStateError
from .simulation import SimulatedTrainer, SimulatedHeartRateSource
from .workout import Workout


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
    def __init__(self) -> None:
        self.clock = SystemClock()
        self.trainer = SimulatedTrainer(clock=self.clock)
        self.hr = SimulatedHeartRateSource(clock=self.clock)
        self.workouts = demo_workouts()
        self.selected = 'baseline'
        self.engine = self._engine()
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.sample: TrainerSample | None = None
        self.generation = 0

    def _engine(self) -> WorkoutEngine:
        return WorkoutEngine(self.trainer, self.hr, self.workouts[self.selected], clock=self.clock)

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
        await self._call(self.engine.prepare())
        self.trainer.emit_sample()
        self.hr.emit_sample()
        await self._call(self.engine.tick())
        self.ready.set()

    async def step(self) -> None:
        async with self.lock:
            self.trainer.emit_sample()
            self.hr.emit_sample()
            if self.engine.state in (WorkoutState.READY, WorkoutState.RUNNING, WorkoutState.PAUSED):
                try:
                    await self._call(self.engine.tick())
                except WorkoutStateError:
                    if self.engine.state != WorkoutState.ABORTED:
                        raise
                self.sample = self.engine.sample
            else:
                while not self.trainer.events.empty():
                    event = self.trainer.events.get_nowait()
                    if isinstance(event, TrainerSample):
                        self.sample = event
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
            engine.events.get_nowait()
            engine.events.task_done()
        return {'state':engine.state, 'elapsed_sec':engine.elapsed_sec,
                'remaining_sec':engine.remaining_sec, 'block':current.name, 'next_block':next_block,
                'planned_target_w':engine.planned_target_w, 'target_w':self.trainer.target_power_w,
                'override_w':engine.override_w, 'recovery_armed':engine.recovery_armed,
                'selected':self.selected,
                'choices':[{'id':key,'name':value.name} for key,value in self.workouts.items()]}

    async def command(self, action: str, selected: str | None = None) -> None:
        if not self.ready.is_set():
            raise ValueError('Simulator wird vorbereitet; bitte kurz warten.')
        generation = self.generation
        if action == 'stop':
            self.generation += 1
            # STOP bypasses the session lock and lets the engine preempt active work.
            await self.engine.stop()
            return
        async with self.lock:
            if generation != self.generation:
                raise ValueError("Befehl durch STOP verworfen.")
            engine = self.engine
            if action == 'select':
                if selected not in self.workouts:
                    raise ValueError('Unbekanntes Workout.')
                if engine.state not in (WorkoutState.READY, WorkoutState.ABORTED, WorkoutState.COMPLETED):
                    raise ValueError('Workout zuerst stoppen, bevor du ein anderes auswählst.')
                await engine.close()
                self.selected = selected
                self.engine = self._engine()
                await self.prepare()
                if generation != self.generation:
                    await self.engine.stop()
            elif action == 'start':
                await self._call(engine.start())
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
