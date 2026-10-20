"""Local definitions are validated before selection; discovery never controls BLE."""
import asyncio
from pathlib import Path

from kickrcoach.workout_catalog import local_workouts
from kickrcoach.web_workout import WebWorkout
from kickrcoach.simulation import ManualClock, SimulatedTrainer, SimulatedHeartRateSource
from kickrcoach.safety import SafetyController


def write_workout(path: Path, *, first: int = 50, steady: int = 130) -> None:
    path.write_text(f'''version: 1
name: Custom workout
notes: Text only; no automatic RPE decision.
blocks:
  - name: Start
    duration_sec: 2
    target:
      watts: {first}
  - name: Main
    duration_sec: 3
    target:
      watts: {steady}
    notes: Observe effort.
''')


def test_catalog_keeps_valid_files_and_reports_invalid_or_unsafe_definitions(tmp_path):
    write_workout(tmp_path/'valid.yaml')
    write_workout(tmp_path/'start-too-high.yml',first=51)
    write_workout(tmp_path/'over-limit.yaml',steady=301)
    (tmp_path/'broken.yaml').write_text('blocks: [')
    (tmp_path/'percent.yaml').write_text('name: Percentage\nblocks:\n- name: A\n  duration_sec: 5\n  target: {percent_baseline: 50.0}\n')
    (tmp_path/'ignore.txt').write_text('not a workout')
    workouts,errors=local_workouts(tmp_path,300)
    assert list(workouts)==['file:valid.yaml']
    assert len(errors)==4
    assert all('nicht geladen' in error for error in errors)
    assert local_workouts(tmp_path/'missing',300)==({},[])
    assert local_workouts(tmp_path/'ignore.txt',300)[1]


def test_loaded_workout_preview_execution_and_snapshot_preserve_definition(tmp_path):
    write_workout(tmp_path/'ride.yaml')
    original=(tmp_path/'ride.yaml').read_bytes()
    async def run():
        clock=ManualClock()
        safety=SafetyController(300)
        trainer=SimulatedTrainer(clock=clock,safety=safety)
        hr=SimulatedHeartRateSource(clock=clock)
        session=WebWorkout(trainer=trainer,hr=hr,workouts_dir=tmp_path)
        session.clock=clock
        session.engine=session._engine()
        try:
            await session.prepare()
            assert trainer.transport.writes==[]
            await session.command('select','file:ride.yaml')
            before=trainer.transport.writes.copy()
            snapshot=session.snapshot()
            assert snapshot['duration_sec']==5
            assert [block['target_w'] for block in snapshot['preview']]==[50,130]
            assert snapshot['workout_notes'].startswith('Text only')
            assert trainer.transport.writes==before
            await session.command('start')
            for _ in range(2):
                clock.advance(1)
                await session.step()
            assert session.snapshot()['target_w']==130
            assert session.snapshot()['block_notes']=='Observe effort.'
            for _ in range(3):
                clock.advance(1)
                await session.step()
            assert session.engine.state=='COMPLETED'
            assert session.engine.elapsed_sec==5
        finally:
            await session.close()
    asyncio.run(run())
    assert (tmp_path/'ride.yaml').read_bytes()==original


def test_web_api_loads_local_directory_and_exposes_preview(tmp_path):
    from fastapi.testclient import TestClient
    from kickrcoach.web import create_app
    write_workout(tmp_path/'ride.yaml')
    headers={'origin':'http://testserver'}
    with TestClient(create_app(workouts_dir=tmp_path)) as client:
        with client.websocket_connect('/ws/telemetry',headers=headers) as ws:
            while True:
                payload=ws.receive_json()
                if payload.get('workout',{}).get('state')=='READY':
                    break
            assert any(choice['id']=='file:ride.yaml' for choice in payload['workout']['choices'])
            selected=client.post('/api/workout',json={'action':'select','selected':'file:ride.yaml'},headers=headers)
            assert selected.status_code==200
            assert selected.json()['state']=='READY'
            assert selected.json()['target_w'] is None
            assert selected.json()['preview'][1]['target_w']==130
            assert selected.json()['workout_notes'].startswith('Text only')
