"""Single workout owner for simulation or explicitly enabled hardware control."""
import asyncio
import logging
from dataclasses import asdict
import sqlite3
from pathlib import Path
from collections.abc import Coroutine

from .domain import SystemClock, TrainerSample, TrainerAdapter, HeartRateSource, ConnectionState as CS, ControlState as TS, StateEvent
from .engine import WorkoutEngine, WorkoutState, WorkoutStateError
from .simulation import SimulatedTrainer, SimulatedHeartRateSource
from .workout import Workout
from .workout_catalog import local_workouts
from .recording import SessionRecorder
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
                 hardware: bool = False, maximum_w: int = 300,
                 stale_sec: float = 5, lost_sec: float = 15,
                 workouts_dir: Path | None = None) -> None:
        self.clock = SystemClock()
        if hardware and (trainer is None or hr is None):
            raise ValueError("hardware sessions require real adapter instances")
        self.stale_sec, self.lost_sec = stale_sec, lost_sec
        self.hardware = hardware
        self.safety = SafetyController(maximum_w)
        self.trainer = trainer if trainer is not None else SimulatedTrainer(clock=self.clock,safety=self.safety)
        self.hr = hr if hr is not None else SimulatedHeartRateSource(clock=self.clock)
        self.workouts = demo_workouts()
        if hardware:
            self.workouts = {key: value.model_copy(update={'name':value.name.replace('Demo · ', '')})
                             for key,value in self.workouts.items()}
        self.catalog_errors: list[str] = []
        if workouts_dir is not None:
            loaded, self.catalog_errors = local_workouts(workouts_dir, maximum_w)
            self.workouts.update(loaded)
        self.notice: str | None = None
        self.selected = 'baseline'
        self.engine = self._engine()
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.sample: TrainerSample | None = None
        self.generation = 0
        self.recorder: SessionRecorder | None = None
        self.recording_error: str | None = None
        self._record_lock = asyncio.Lock()
        self._recorded_engine: WorkoutEngine | None = None

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
        try:
            await self._step()
        finally:
            await self.record()

    async def _step(self) -> None:
        async with self.lock:
            if not self.hardware:
                self.trainer.emit_sample()
                self.hr.emit_sample()
            elif (self.engine.state == WorkoutState.IDLE and self.trainer.connection == CS.CONNECTED
                  and self.trainer.control_state == TS.READONLY):
                try:
                    await self.prepare()
                except (ValueError, RuntimeError) as exc:
                    if not self.notice or 'STOP wurde nicht bestätigt' not in self.notice:
                        self.notice = f"Workout nicht bereit: {exc}"
                    self.ready.set()
            elif self.engine.state == WorkoutState.READY and self.trainer.connection != CS.CONNECTED:
                await self.record()
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
        return {'state':engine.state, 'mode':'hardware' if self.hardware else 'simulation',
                'notice':self.notice, 'recording_error':self.recording_error,
                'session_id':self.recorder.session_id if self.recorder else None, 'maximum_w':self.safety.maximum_w,
                'can_select':engine.state in (WorkoutState.IDLE, WorkoutState.READY, WorkoutState.ABORTED, WorkoutState.COMPLETED),
                'can_start':engine.state == WorkoutState.READY and self.trainer.connection == CS.CONNECTED
                            and self.trainer.control_state == TS.READONLY,
                'can_resume':engine.state == WorkoutState.PAUSED and self.trainer.connection == CS.CONNECTED and
                             (not engine.recovery_required or engine.recovery_armed), 'elapsed_sec':engine.elapsed_sec,
                'remaining_sec':engine.remaining_sec, 'block':current.name, 'next_block':next_block,
                'planned_target_w':engine.planned_target_w if engine.planned_target_w is not None
                                   else current.target.resolve(engine.baseline_w),
                'target_w':self.trainer.target_power_w,
                'override_w':engine.override_w, 'recovery_armed':engine.recovery_armed,
                'selected':self.selected, 'catalog_errors':self.catalog_errors,
                'duration_sec':engine.workout.duration_sec, 'workout_notes':engine.workout.notes,
                'block_notes':current.notes,
                'cadence_target':current.cadence.model_dump() if current.cadence else None,
                'power_tolerance_w':5,
                'preview':[{'name':block.name, 'duration_sec':block.duration_sec,
                            'target_w':block.target.resolve(engine.baseline_w),
                            'cadence':block.cadence.model_dump() if block.cadence else None} for block in engine.workout.blocks],
                'choices':[{'id':key,'name':value.name} for key,value in self.workouts.items()]}

    async def command(self, action: str, selected: str | None = None) -> None:
        try:
            await self._command(action, selected)
        finally:
            await self.record()

    async def _command(self, action: str, selected: str | None = None) -> None:
        generation = self.generation
        if action == 'stop':
            self.generation += 1
            # STOP bypasses the session lock and lets the engine preempt active work.
            await self.engine.stop()
            self.ready.set()
            return
        if action != 'select' and not self.ready.is_set():
            raise ValueError('Warte auf Trainer und Workout-Vorbereitung.')
        async with self.lock:
            if generation != self.generation:
                raise ValueError("Befehl durch STOP verworfen.")
            engine = self.engine
            if action == 'select':
                if selected not in self.workouts:
                    raise ValueError('Unbekanntes Workout.')
                if engine.state not in (WorkoutState.IDLE, WorkoutState.READY, WorkoutState.ABORTED, WorkoutState.COMPLETED):
                    raise ValueError('Workout zuerst stoppen, bevor du ein anderes auswählst.')
                if not self.hardware:
                    await engine.close()
                if not self.notice or 'STOP wurde nicht bestätigt' not in self.notice:
                    self.notice = None
                await self.record()
                self.selected = selected
                self.engine = self._engine()
                self.ready.clear()
                # Selection is local. Device capability validation happens only
                # once a confirmed read-only connection is available.
                if (not self.hardware or (self.trainer.connection == CS.CONNECTED
                                          and self.trainer.control_state == TS.READONLY)):
                    await self.prepare()
                if generation != self.generation:
                    await self.engine.stop()
            elif action == 'start':
                if self.recording_error:
                    raise ValueError('Aufzeichnung nicht verfügbar. Speicher prüfen und App neu starten.')
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
        await self.record()

    async def record(self) -> None:
        # Persistence runs off the event loop; STOP never waits for this lock
        # before invalidating commands and attempting trainer stop.
        async with self._record_lock:
            engine = self.engine
            if self.recorder is not None and self._recorded_engine is not engine:
                # A newly selected/prepared engine has no ride yet. Its cleanup
                # must never relabel the preceding completed session as aborted.
                self.recorder.session_id = None
                self.recorder.active = False
                self._recorded_engine = engine
            events = []
            while not engine.events.empty():
                event = engine.events.get_nowait()
                events.append(asdict(event))
                if 'stop_unconfirmed' in event.reason or 'physical_stop' in event.reason:
                    self.notice = 'STOP wurde nicht bestätigt. Treten beenden und Trainer entlasten.'
                    log.error(self.notice)
                engine.events.task_done()
            if self.recorder is None or self.recording_error:
                return
            now = self.clock.monotonic()
            sample = engine.sample
            age = max(0, now-sample.received.monotonic) if sample else None
            freshness = ('LOST' if self.trainer.connection != CS.CONNECTED or age is None or age >= self.lost_sec
                         else 'STALE' if age >= self.stale_sec else 'LIVE')
            hr = self.hr.latest
            point = {
                'dropped_engine_events':engine.dropped_events,
                'stop_confirmed':self.trainer.stop_confirmed,
                'cadence_target':engine.workout.blocks[engine.block_index].cadence.model_dump()
                                 if engine.workout.blocks[engine.block_index].cadence else None,
                'block_index':engine.block_index, 'block':engine.workout.blocks[engine.block_index].name,
                'planned_target_w':engine.planned_target_w, 'commanded_target_w':self.trainer.target_power_w,
                'override_w':engine.override_w,
                'power_w':sample.power_w if sample and freshness == 'LIVE' else None,
                'cadence_rpm':sample.cadence_rpm if sample and freshness == 'LIVE' else None,
                'trainer_received_at':sample.received.utc.isoformat() if sample else None,
                'trainer_received_monotonic':sample.received.monotonic if sample else None,
                'trainer_age_sec':age, 'trainer_freshness':freshness,
                'trainer_connection':self.trainer.connection, 'trainer_control':self.trainer.control_state,
                'bpm':hr.bpm if hr.freshness == 'LIVE' and self.hr.connection == CS.CONNECTED else None,
                'hr_received_at':hr.received.utc.isoformat() if hr.received else None,
                'hr_received_monotonic':hr.received.monotonic if hr.received else None,
                'hr_age_sec':max(0, now-hr.received.monotonic) if hr.received else None,
                'hr_freshness':hr.freshness, 'hr_connection':self.hr.connection,
            }
            try:
                write = asyncio.create_task(asyncio.to_thread(self.recorder.update,
                    now=now, utc=self.clock.utcnow(), workout=engine.workout.model_dump(mode='json'),
                    state=engine.state, elapsed=engine.elapsed_sec, events=events, sample=point))
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    # to_thread cannot cancel a database transaction. Join before
                    # shutdown's final recording pass can touch this recorder.
                    await write
                    raise
            except (sqlite3.Error, OSError):
                self.recording_error = 'Aufzeichnung fehlgeschlagen. Speicher prüfen; diese Einheit ist möglicherweise unvollständig.'
                log.error('Session recording failed (private details omitted)')
                if self.recorder.session_id:
                    await asyncio.to_thread(self.recorder.store.record_failure, self.recorder.session_id)
