"""M2 acceptance and failure cases with virtual time and real M1 adapter code."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
import pytest
from pydantic import ValidationError
import yaml

from kickrcoach.engine import WorkoutEngine, WorkoutState as WS, WorkoutStateError
from kickrcoach.domain import Freshness
from kickrcoach.protocol import POWER_RANGE
from kickrcoach.simulation import ManualClock, SimulatedTrainer, SimulatedHeartRateSource
from kickrcoach.workout import Workout, load_workout
from kickrcoach.workout_safety import WorkoutSafetyConfig


def definition(*, targets=(40, 70, 40), duration=10, cadence=None):
    return Workout.model_validate({'name':'fixture', 'blocks':[
        {'name':f'block {i}', 'duration_sec':duration, 'target':{'watts':target},
         **({'cadence':cadence} if cadence else {})}
        for i, target in enumerate(targets)]})


@asynccontextmanager
async def session(workout=None, *, start=True, baseline_w=None):
    clock = ManualClock()
    trainer = SimulatedTrainer(clock=clock, command_timeout_sec=.05)
    hr = SimulatedHeartRateSource(clock=clock)
    engine = WorkoutEngine(trainer, hr, workout or definition(), clock=clock, baseline_w=baseline_w)
    try:
        await engine.prepare()
        trainer.emit_sample()
        hr.emit_sample()
        if start:
            await engine.start()
        yield engine, trainer, hr, clock
    finally:
        await engine.close()


async def advance(engine, trainer, hr, clock, seconds=1, cadence=80):
    clock.advance(seconds)
    trainer.emit_sample(cadence_rpm=cadence)
    hr.emit_sample()
    await engine.tick()


def powers(trainer):
    return [int.from_bytes(w[1:], 'little', signed=True) for w in trainer.transport.writes if w[0] == 5]


@pytest.mark.parametrize('block', [
    {'duration_sec':0, 'target':{'watts':40}},
    {'duration_sec':True, 'target':{'watts':40}},
    {'duration_sec':10, 'target':{'watts':True}},
    {'duration_sec':10, 'target':{'watts':40, 'percent_baseline':50}},
    {'duration_sec':10, 'target':{}},
    {'duration_sec':10, 'target':{'watts':40}, 'mode':'resistance'},
    {'duration_sec':10, 'target':{'watts':40}, 'extra':1},
    {'duration_sec':10, 'target':{'percent_baseline':float('nan')}},
    {'duration_sec':10, 'target':{'watts':40}, 'cadence':{'minimum_rpm':90,'maximum_rpm':70}},
])
def test_rejects_invalid_definitions(block):
    with pytest.raises(ValidationError):
        Workout.model_validate({'name':'bad', 'blocks':[{'name':'block', **block}]})


def test_yaml_and_immutable_snapshot(tmp_path):
    path = tmp_path / 'ride.yaml'
    path.write_text('name: Example\nblocks:\n - name: Easy\n   duration_sec: 10\n   target: {percent_baseline: 50}\n')
    workout = load_workout(path)
    assert workout.blocks[0].target.resolve(120) == 60
    with pytest.raises(ValueError):
        workout.blocks[0].target.resolve(None)
    with pytest.raises(ValidationError):
        workout.blocks[0].duration_sec = 99
    path.write_text('not the same definition')
    assert workout.duration_sec == 10
    path.write_text('name: one\nname: two\n')
    with pytest.raises(ValueError, match='duplicate'):
        load_workout(path)
    path.write_text('!!python/object/apply:os.system ["echo unsafe"]')
    with pytest.raises(yaml.YAMLError):
        load_workout(path)
    path.write_bytes(b'x' * 262145)
    with pytest.raises(ValueError, match='256 KiB'):
        load_workout(path)
    assert load_workout(Path('workouts/baseline.yaml')).duration_sec == 240


def test_complete_multiblock_workout_exact_boundaries_and_no_wall_clock_dependency():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            assert powers(trainer) == [40]
            clock.start += timedelta(days=365)  # Wall time must not affect scheduling.
            await advance(engine, trainer, hr, clock, 9)
            assert engine.block_index == 0
            await advance(engine, trainer, hr, clock)
            assert engine.block_index == 1 and trainer.target_power_w == 70
            await advance(engine, trainer, hr, clock, 10)
            assert engine.block_index == 2
            await advance(engine, trainer, hr, clock, 10)
            assert engine.state == WS.COMPLETED
            assert engine.elapsed_sec == 30 and engine.remaining_sec == 0
            assert powers(trainer) == [40, 70, 40]
            assert trainer.transport.writes[-1] == b'\x08\x01'
    asyncio.run(run())


def test_pause_freezes_time_resume_and_block_scoped_override():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            await advance(engine, trainer, hr, clock, 4)
            await engine.adjust(5)
            assert engine.override_w == 45
            await engine.pause()
            await advance(engine, trainer, hr, clock, 60)
            assert engine.state == WS.PAUSED and engine.elapsed_sec == 4
            await engine.resume()
            await advance(engine, trainer, hr, clock, 6)
            assert engine.elapsed_sec == 10 and engine.block_index == 1
            assert engine.override_w is None and trainer.target_power_w == 70
    asyncio.run(run())


def test_prepare_is_readonly_and_start_requires_fresh_safe_cadence():
    async def run():
        async with session(start=False) as (engine, trainer, hr, clock):
            assert trainer.transport.writes == []
            trainer.emit_sample(cadence_rpm=0)
            with pytest.raises(WorkoutStateError):
                await engine.start()
            assert engine.state == WS.READY and trainer.transport.writes == []
            trainer.emit_sample()
            clock.advance(5)
            with pytest.raises(WorkoutStateError):
                await engine.start()
            assert trainer.transport.writes == []
    asyncio.run(run())


def test_manual_bounds_rejection_keeps_healthy_workout_running():
    async def run():
        async with session(definition(targets=(100,))) as (engine, trainer, hr, clock):
            with pytest.raises(WorkoutStateError):
                await engine.adjust(5)
            assert engine.state == WS.RUNNING and trainer.target_power_w == 100
            assert powers(trainer) == [100]
            await engine.adjust(-5)
            assert trainer.target_power_w == 95
            with pytest.raises(ValueError):
                await engine.adjust(20)
    asyncio.run(run())


def test_preflight_checks_every_future_target_before_request_control():
    async def run():
        clock = ManualClock()
        trainer = SimulatedTrainer(clock=clock)
        hr = SimulatedHeartRateSource(clock=clock)
        engine = WorkoutEngine(trainer, hr, definition(targets=(40, 105)), clock=clock)
        try:
            with pytest.raises(ValueError):
                await engine.prepare()
            assert engine.state == WS.ABORTED
            assert trainer.transport.writes == []
        finally:
            await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize('scenario', ['timeout','rejected_control'])
def test_protocol_failure_never_enters_running(scenario):
    async def run():
        async with session(start=False) as (engine, trainer, hr, clock):
            setattr(trainer.faults, scenario, True)
            with pytest.raises((TimeoutError, RuntimeError)):
                await engine.start()
            assert engine.state == WS.PAUSED and engine.recovery_required
            assert powers(trainer) == []
            assert engine.elapsed_sec == 0
    asyncio.run(run())


def test_cadence_collapse_pauses_after_sustained_threshold_and_blocks_resume():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            for _ in range(3):
                await advance(engine, trainer, hr, clock, cadence=0)
                assert engine.state == WS.RUNNING
            await advance(engine, trainer, hr, clock, cadence=0)
            assert engine.state == WS.PAUSED
            assert trainer.transport.writes[-1] == b'\x08\x02'
            frozen = engine.elapsed_sec
            with pytest.raises(WorkoutStateError):
                await engine.resume()
            await advance(engine, trainer, hr, clock, 30, cadence=80)
            assert engine.elapsed_sec == frozen
            await engine.resume()
            assert engine.state == WS.RUNNING
    asyncio.run(run())


def test_intentional_low_cadence_not_universally_rejected_and_anti_stall_remains():
    async def run():
        workout = definition(targets=(40,), duration=60,
                             cadence={'minimum_rpm':30, 'maximum_rpm':45})
        async with session(workout) as (engine, trainer, hr, clock):
            for _ in range(5):
                await advance(engine, trainer, hr, clock, cadence=30)
            assert engine.state == WS.RUNNING
            for _ in range(4):
                await advance(engine, trainer, hr, clock, cadence=0)
            assert engine.state == WS.PAUSED
    asyncio.run(run())


def test_silent_trainer_and_missing_fields_pause_without_carrying_values():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            clock.advance(5)
            await engine.tick()
            assert engine.state == WS.PAUSED
            assert engine.sample is not None
        async with session() as (engine, trainer, hr, clock):
            from kickrcoach.domain import TrainerSample, ReceivedAt
            trainer.events.put_nowait(TrainerSample(ReceivedAt.now(clock), power_w=40))
            await engine.tick()
            assert engine.state == WS.PAUSED
    asyncio.run(run())


def test_hr_loss_does_not_change_power_or_stop_static_workout():
    async def run():
        async with session(definition(targets=(70,), duration=60)) as (engine, trainer, hr, clock):
            hr.faults.silent_hr = True
            await advance(engine, trainer, hr, clock, 15)
            assert hr.latest.freshness == Freshness.LOST and hr.latest.bpm is None
            assert engine.state == WS.RUNNING and powers(trainer) == [70]
            hr.lose_connection()
            await advance(engine, trainer, hr, clock)
            await asyncio.sleep(0)
            assert engine.state == WS.RUNNING and powers(trainer) == [70]
    asyncio.run(run())


def test_disconnect_freezes_time_safe_recovery_requires_explicit_resume():
    async def run():
        async with session(definition(targets=(90,40),duration=30)) as (engine, trainer, hr, clock):
            await advance(engine, trainer, hr, clock, 4)
            old_transport = trainer.transport
            trainer.lose_connection()
            await engine.tick()
            assert engine.state == WS.PAUSED and engine.elapsed_sec == 4
            clock.advance(20)
            await engine.tick()
            assert engine.recovery_armed and engine.elapsed_sec == 4
            assert trainer.transport is not old_transport
            assert powers(trainer) == [50]  # Never replay 90 W on reconnect.
            assert all(w[0] != 7 for w in trainer.transport.writes)
            with pytest.raises(WorkoutStateError):
                await engine.resume()  # New-session telemetry has not arrived.
            trainer.emit_sample()
            await engine.resume()
            assert engine.state == WS.RUNNING and trainer.target_power_w == 50
            await advance(engine, trainer, hr, clock, 26)
            assert trainer.target_power_w == 40 and engine.override_w is None
    asyncio.run(run())


def test_stop_preempts_pending_target_and_rejects_queued_adjustment():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            entered = asyncio.Event()
            original = trainer.transport.write_gatt_char
            async def delayed(characteristic, data, response=True):
                if data[0] == 5:
                    entered.set()
                    await asyncio.Event().wait()
                await original(characteristic, data, response)
            trainer.transport.write_gatt_char = delayed
            pending = asyncio.create_task(engine.adjust(5))
            await entered.wait()
            queued = asyncio.create_task(engine.adjust(5))
            await asyncio.sleep(0)
            await engine.stop()
            results = await asyncio.gather(pending, queued, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert isinstance(results[1], WorkoutStateError)
            assert engine.state == WS.ABORTED
            assert powers(trainer) == [40]
            assert trainer.transport.writes[-1] == b'\x08\x01'
            clock.advance(60)
            assert engine.elapsed_sec == 0
            with pytest.raises(WorkoutStateError):
                await engine.resume()
            with pytest.raises(WorkoutStateError):
                await engine.tick()
    asyncio.run(run())


def test_local_abort_succeeds_even_when_ftms_stop_fails():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            async def fail():
                raise ConnectionError('radio lost')
            trainer.stop = fail
            await engine.stop()
            assert engine.state == WS.ABORTED and engine._since is None
            reasons = []
            while not engine.events.empty():
                reasons.append(engine.events.get_nowait().reason)
            assert any('stop_unconfirmed' in reason for reason in reasons)
    asyncio.run(run())


def test_hr_reconnect_does_not_block_safety_ticks_and_is_cancelled_on_close():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            entered = asyncio.Event()
            async def stalled_connect():
                entered.set()
                await asyncio.Event().wait()
            hr.lose_connection()
            hr.connect = stalled_connect
            await engine.tick()
            await entered.wait()
            clock.advance(5)
            await engine.tick()
            assert engine.state == WS.PAUSED  # Trainer staleness still detected.
        assert engine._hr_reconnect_task.done()
    asyncio.run(run())


@pytest.mark.parametrize('kwargs', [{'cadence_collapse_sec':0}, {'telemetry_timeout_sec':float('nan')},
                                    {'recovery_target_w':200}, {'anti_stall_rpm':40}])
def test_invalid_safety_config(kwargs):
    with pytest.raises(ValueError):
        WorkoutSafetyConfig(**kwargs)


def test_block_command_failure_freezes_and_requires_recovery():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            trainer.faults.timeout = True
            with pytest.raises(TimeoutError):
                await advance(engine, trainer, hr, clock, 10)
            assert engine.state == WS.PAUSED and engine.recovery_required
            frozen = engine.elapsed_sec
            clock.advance(10)
            assert engine.elapsed_sec == frozen
            assert trainer.target_power_w is None
    asyncio.run(run())


def test_every_target_path_is_validated_by_shared_safety_controller():
    from kickrcoach.safety import SafetyController
    validated = []
    class RecordingSafety(SafetyController):
        def validate(self, watts, supported):
            validated.append(watts)
            return super().validate(watts, supported)
    async def run():
        async with session(start=False) as (engine, trainer, hr, clock):
            engine.safety = RecordingSafety()
            await engine.start()
            await engine.adjust(5)
            await advance(engine, trainer, hr, clock, 10)
            trainer.lose_connection()
            await engine.tick()
            await engine.tick()
            assert validated == [40, 45, 45, 70, 50]
    asyncio.run(run())


def test_failed_recovery_backoff_and_stop_during_retry():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            trainer.lose_connection()
            await engine.tick()
            attempts = []
            async def fail():
                attempts.append(clock.monotonic())
                raise ConnectionError('still offline')
            trainer.connect = fail
            await engine.tick()
            assert not engine.recovery_armed and engine.state == WS.PAUSED
            for _ in range(4):
                clock.advance(1)
                await engine.tick()
            assert attempts == [0]
            clock.advance(1)
            await engine.tick()
            assert attempts == [0,5]
            entered = asyncio.Event()
            async def pending():
                entered.set()
                await asyncio.Event().wait()
            trainer.connect = pending
            clock.advance(5)
            task = asyncio.create_task(engine.tick())
            await entered.wait()
            await engine.stop()
            await asyncio.gather(task, return_exceptions=True)
            assert engine.state == WS.ABORTED and not engine.recovery_armed
            assert engine.elapsed_sec == 0
    asyncio.run(run())


def test_percentage_target_uses_explicit_baseline_and_rounded_watts():
    async def run():
        workout = Workout.model_validate({'name':'percent', 'blocks':[
            {'name':'easy','duration_sec':10,'target':{'percent_baseline':50}}]})
        async with session(workout, baseline_w=121) as (engine, trainer, hr, clock):
            assert powers(trainer) == [61]
    asyncio.run(run())


def test_stop_clears_pending_cadence_guard_and_has_bounded_events():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            for _ in range(300):
                engine._event('diagnostic')
            assert engine.events.qsize() == 256 and engine.dropped_events > 0
            await engine.stop()
            assert engine.state == WS.ABORTED
            with pytest.raises(WorkoutStateError):
                await engine.start()
            assert powers(trainer) == [40]
    asyncio.run(run())


def test_stop_does_not_deadlock_when_cancelled_caller_closes_in_finally():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            entered = asyncio.Event()
            original = trainer.transport.write_gatt_char
            async def delayed(characteristic, data, response=True):
                if data[0] == 5:
                    entered.set()
                    await asyncio.Event().wait()
                await original(characteristic, data, response)
            trainer.transport.write_gatt_char = delayed
            async def caller():
                try:
                    await engine.adjust(5)
                finally:
                    await engine.close()
            task = asyncio.create_task(caller())
            await entered.wait()
            await asyncio.wait_for(engine.stop(), timeout=1)
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
            assert engine.state == WS.ABORTED
            assert not trainer.transport.is_connected
    asyncio.run(run())


@pytest.mark.parametrize('version', [True, '1', 2])
def test_workout_version_is_explicit_and_strict(version):
    with pytest.raises(ValidationError):
        Workout.model_validate({'version':version,'name':'bad', 'blocks':[
            {'name':'easy', 'duration_sec':10, 'target':{'watts':40}}]})


def test_recovery_rounds_down_to_supported_increment():
    import struct
    async def run():
        async with session(definition(targets=(90,))) as (engine, trainer, hr, clock):
            trainer.lose_connection()
            await engine.tick()
            factory = trainer.factory
            def ranged_factory(callback):
                transport = factory(callback)
                original = transport.read_gatt_char
                async def read(characteristic):
                    if characteristic == POWER_RANGE:
                        return struct.pack('<hhH', 3, 999, 7)
                    return await original(characteristic)
                transport.read_gatt_char = read
                return transport
            trainer.factory = ranged_factory
            await engine.tick()
            assert engine.recovery_armed and powers(trainer) == [45]
    asyncio.run(run())


def test_ready_does_not_run_and_completed_cannot_be_resumed_or_adjusted():
    async def run():
        async with session(definition(targets=(40,)), start=False) as (engine, trainer, hr, clock):
            await advance(engine, trainer, hr, clock, 50)
            assert engine.state == WS.READY and engine.elapsed_sec == 0
            assert trainer.transport.writes == []
            await engine.start()
            await advance(engine, trainer, hr, clock, 10)
            assert engine.state == WS.COMPLETED
            for operation in (engine.resume(), engine.adjust(5)):
                with pytest.raises(WorkoutStateError):
                    await operation
    asyncio.run(run())


def test_late_tick_skips_expired_blocks_without_replaying_commands():
    async def run():
        async with session() as (engine, trainer, hr, clock):
            await advance(engine, trainer, hr, clock, 25)
            assert engine.block_index == 2 and engine.elapsed_sec == 25
            assert powers(trainer) == [40,40]
    asyncio.run(run())


@pytest.mark.parametrize('maximum', [True, 0, 32768, 50.5])
def test_final_safety_configuration_rejects_invalid_bounds(maximum):
    from kickrcoach.safety import SafetyController
    with pytest.raises(ValueError):
        SafetyController(maximum)


def test_requested_start_waits_without_writes_or_elapsed_time_until_pedaling():
    async def run():
        async with session(start=False) as (engine,trainer,hr,clock):
            trainer.emit_sample(cadence_rpm=0)
            await engine.start(wait_for_pedaling=True)
            assert engine.state==WS.WAITING and trainer.transport.writes==[]
            for _ in range(3):
                await advance(engine,trainer,hr,clock,30,cadence=0)
                assert engine.state==WS.WAITING and engine.elapsed_sec==0
                assert engine.remaining_sec==30 and trainer.transport.writes==[]
            # A missing or stale sample cannot start resistance either.
            clock.advance(15)
            await engine.tick()
            assert engine.state==WS.WAITING and trainer.transport.writes==[]
            await advance(engine,trainer,hr,clock,1,cadence=80)
            assert engine.state==WS.RUNNING and engine.elapsed_sec==0
            assert trainer.transport.writes[:3]==[b'\x00',b'\x05\x28\x00',b'\x07']
            await advance(engine,trainer,hr,clock,10)
            assert engine.block_index==1 and engine.elapsed_sec==10
    asyncio.run(run())


def test_stop_while_waiting_prevents_later_pedaling_from_starting():
    async def run():
        async with session(start=False) as (engine,trainer,hr,clock):
            trainer.emit_sample(cadence_rpm=0)
            await engine.start(wait_for_pedaling=True)
            await engine.stop()
            trainer.emit_sample(cadence_rpm=80)
            with pytest.raises(WorkoutStateError):
                await engine.tick()
            assert engine.state==WS.ABORTED and engine.elapsed_sec==0
            assert trainer.transport.writes==[]
    asyncio.run(run())


def test_waiting_start_survives_a_pre_start_connection_loss_without_restoring_control():
    async def run():
        async with session(start=False) as (engine,trainer,hr,clock):
            trainer.emit_sample(cadence_rpm=0)
            await engine.start(wait_for_pedaling=True)
            trainer.lose_connection()
            await engine.tick()
            assert engine.state==WS.WAITING and engine.elapsed_sec==0
            await trainer.disconnect()
            await trainer.connect()
            await engine.tick()
            assert trainer.transport.writes==[]
            trainer.emit_sample(cadence_rpm=80)
            await engine.tick()
            assert engine.state==WS.RUNNING and engine.elapsed_sec==0
    asyncio.run(run())


def test_training_safety_accepts_over_100_but_respects_config_and_device_range():
    from kickrcoach.safety import SafetyController
    from kickrcoach.protocol import PowerRange
    safety=SafetyController(300)
    assert safety.validate(250,PowerRange(0,1000,5))==250
    for watts,limits in [(305,PowerRange(0,1000,5)),(250,PowerRange(0,200,5)),
                         (251,PowerRange(0,1000,5)),(True,PowerRange(0,1000,1))]:
        with pytest.raises(ValueError):
            safety.validate(watts,limits)
    # M0 diagnostics still refuse a configured limit above 100 W.
    from kickrcoach.cli import Config
    with pytest.raises(ValueError,match='M0'):
        Config(maximum_w=101).validate()
    Config(training_maximum_w=300).validate()


@pytest.mark.parametrize('limit',[True,0,2001,300.5])
def test_training_limit_validation(limit):
    from kickrcoach.cli import Config
    with pytest.raises(ValueError,match='training_maximum_w'):
        Config(training_maximum_w=limit).validate()
