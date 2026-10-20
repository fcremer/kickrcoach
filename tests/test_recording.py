"""Deterministic archive/export checks; all trainer commands use memory transports."""
import asyncio
from datetime import datetime, timezone
import sqlite3
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from kickrcoach.recording import SessionStore, SessionRecorder, export_tcx
from kickrcoach.simulation import ManualClock, SimulatedTrainer, SimulatedHeartRateSource
from kickrcoach.web_workout import WebWorkout
from kickrcoach.workout import Workout
from kickrcoach.web import create_app
from kickrcoach.telemetry import Telemetry


async def owner(tmp_path, *, simulated=False):
    clock = ManualClock()
    trainer, hr = SimulatedTrainer(clock=clock), SimulatedHeartRateSource(clock=clock)
    session = WebWorkout(trainer=trainer, hr=hr, hardware=True)
    session.clock = clock
    session.workouts['baseline'] = Workout.model_validate({'name':'Test & <ride>', 'blocks':[
        {'name':'Easy', 'duration_sec':5, 'target':{'watts':40}},
        {'name':'Steady', 'duration_sec':5, 'target':{'watts':70}}]})
    session.engine = session._engine()
    store = SessionStore(tmp_path/'sessions.sqlite3')
    session.recorder = SessionRecorder(store, simulated=simulated)
    await trainer.connect()
    await hr.connect()
    await session.prepare()
    await session.record()
    return clock, session, store


async def pedaling(session, clock, seconds=0, bpm=125):
    clock.advance(seconds)
    session.trainer.emit_sample(power_w=42, cadence_rpm=82)
    if bpm is not None:
        session.hr.emit_sample(bpm)
    await session.step()


def test_wait_pause_override_completion_and_reopen(tmp_path):
    async def run():
        clock, session, store = await owner(tmp_path)
        assert store.list_sessions() == []
        await session.command('start')
        assert session.engine.state == 'WAITING'
        assert session.trainer.transport.writes == []
        await pedaling(session,clock,60)
        assert session.engine.state == 'RUNNING'
        for _ in range(2):
            await pedaling(session,clock,1)
        await session.command('increase')
        await session.command('pause')
        await pedaling(session,clock,10)
        await session.command('resume')
        for _ in range(8):
            await pedaling(session,clock,1)
        assert session.engine.state == 'COMPLETED'
        identifier = session.recorder.session_id
        data = store.detail(identifier)
        assert data['status'] == 'COMPLETED'
        assert data['summary']['active_duration_sec'] == 10
        assert data['summary']['waiting_duration_sec'] == 60
        assert data['summary']['paused_duration_sec'] == 10
        assert data['summary']['riding_wall_duration_sec'] == 20
        assert data['summary']['average_power_w'] == 42
        assert data['summary']['average_bpm'] == 125
        assert data['summary']['coverage_power_w_sec'] == 10
        assert any(event['reason']=='manual_pause' for event in data['events'])
        assert any(event['reason'].startswith('target_confirmed:manual') for event in data['events'])
        assert any(point['override_w'] == 45 for point in data['samples'])
        assert any(point['state'] == 'WAITING' for point in data['samples'])
        assert data['metadata']['workout']['blocks'][1]['target']['watts'] == 70
        await session.close()
        reopened = SessionStore(store.path).detail(identifier)
        assert reopened['status'] == 'COMPLETED'
        assert reopened['samples'] == data['samples']
        xml = export_tcx(reopened)
        root = ET.fromstring(xml)
        ns = {'t':'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2',
              'a':'http://www.garmin.com/xmlschemas/ActivityExtension/v2'}
        assert len(root.findall('.//t:Track',ns)) == 2
        assert float(root.find('.//t:TotalTimeSeconds',ns).text) == 10
        assert {x.text for x in root.findall('.//a:Watts',ns)} == {'42'}
        assert {x.text for x in root.findall('.//t:HeartRateBpm/t:Value',ns)} == {'125'}
        assert len(root.findall('.//t:Trackpoint',ns)) == data['summary']['running_sample_count'] + 1
        assert root.find('.//t:Position',ns) is None
        assert 'Test &amp; &lt;ride&gt;' in xml.decode()
    asyncio.run(run())


def test_stale_hr_is_null_and_never_exported_as_zero(tmp_path):
    async def run():
        clock, session, store = await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        for _ in range(6):
            await pedaling(session,clock,1,bpm=None)
        await session.command('stop')
        data = store.detail(session.recorder.session_id)
        stale = [point for point in data['samples'] if point['hr_freshness'] != 'LIVE']
        assert stale and all(point['bpm'] is None for point in stale)
        assert data['summary']['average_bpm'] == 125
        assert data['summary']['coverage_bpm_sec'] < data['summary']['active_duration_sec']
        assert b'<Value>0</Value>' not in export_tcx(data)
        await session.close()
    asyncio.run(run())


def test_crash_recovery_marks_interrupted_without_resuming(tmp_path):
    async def run():
        clock, session, store = await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        await pedaling(session,clock,1)
        identifier = session.recorder.session_id
        recovered = SessionStore(store.path).detail(identifier)
        assert recovered['status'] == 'INTERRUPTED'
        assert recovered['samples']
        assert export_tcx(recovered)
        await session.close()
    asyncio.run(run())


def test_storage_failure_surfaces_but_does_not_prevent_stop(tmp_path, monkeypatch):
    async def run():
        clock, session, store = await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        monkeypatch.setattr(store,'append',lambda *_args:(_ for _ in ()).throw(sqlite3.OperationalError('full')))
        await pedaling(session,clock,1)
        assert session.snapshot()['recording_error']
        assert store.detail(session.recorder.session_id)['status'] == 'RECORDING_FAILED'
        await session.command('stop')
        assert session.engine.state == 'ABORTED'
        assert session.trainer.target_power_w is None
        await session.close()
    asyncio.run(run())


def test_stop_does_not_wait_for_slow_disk_before_stopping_trainer(tmp_path,monkeypatch):
    import threading
    async def run():
        clock, session, store = await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        entered, release = threading.Event(), threading.Event()
        original = store.append
        def slow(*args):
            entered.set()
            assert release.wait(3)
            original(*args)
        monkeypatch.setattr(store,'append',slow)
        task = asyncio.create_task(pedaling(session,clock,1))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            stop = asyncio.create_task(session.command('stop'))
            for _ in range(100):
                if session.trainer.target_power_w is None:
                    break
                await asyncio.sleep(.001)
            assert session.engine.state == 'ABORTED'
            assert session.trainer.target_power_w is None
        finally:
            release.set()
        await task
        await stop
        await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('simulated,waiting',[(True,False),(False,True)])
def test_tcx_rejects_simulation_and_unridden_start(tmp_path,simulated,waiting):
    async def run():
        clock, session, store = await owner(tmp_path,simulated=simulated)
        if not waiting:
            await pedaling(session,clock)
        await session.command('start')
        with pytest.raises(ValueError,match='beenden'):
            export_tcx(store.detail(session.recorder.session_id))
        await session.command('stop')
        with pytest.raises(ValueError):
            export_tcx(store.detail(session.recorder.session_id))
        await session.close()
    asyncio.run(run())


def test_archive_api_download_feedback_and_missing_id(tmp_path):
    store = SessionStore(tmp_path/'sessions.sqlite3')
    identifier = store.begin({'workout':{'name':'Fixture'},'simulated':False},datetime.now(timezone.utc).isoformat())
    store.append(identifier,[],None,'ABORTED')
    # Inject read-only telemetry: browsing/downloading never acquires control.
    trainer, hr = SimulatedTrainer(), SimulatedHeartRateSource()
    service = Telemetry(trainer,hr,simulated=True)
    with TestClient(create_app(telemetry=service,data_dir=tmp_path)) as client:
        assert client.get('/api/sessions').json()[0]['id'] == identifier
        detail = client.get(f'/api/sessions/{identifier}')
        assert detail.headers['cache-control'] == 'no-store'
        assert detail.json()['status'] == 'ABORTED'
        download = client.get(f'/api/sessions/{identifier}/download/json')
        assert download.json()['summary'] == detail.json()['summary']
        assert download.json()['samples'] == []
        assert 'samples' not in detail.json()
        assert 'attachment;' in download.headers['content-disposition']
        assert client.get(f'/api/sessions/{identifier}/download/tcx').status_code == 409
        assert client.get('/api/sessions/missing').status_code == 404
        endpoint = f'/api/sessions/{identifier}/feedback'
        assert client.post(endpoint,json={'rpe':5}).status_code == 403
        headers={'origin':'http://testserver'}
        assert client.post(endpoint,json={'rpe':11},headers=headers).status_code == 422
        feedback = client.post(endpoint,json={'rpe':5,'cooling':True,'notes':'Good'},headers=headers)
        assert feedback.status_code == 200
        assert feedback.json()['metadata']['rpe'] == 5
        assert client.get(f'/api/sessions/{identifier}/download/json').json()['metadata']['notes'] == 'Good'
    assert trainer.transport.writes == []


def test_archive_lease_prevents_second_process_recovery(tmp_path):
    from kickrcoach.recording import archive_lease
    with archive_lease(tmp_path):
        with pytest.raises(RuntimeError,match='anderen App-Instanz'):
            with archive_lease(tmp_path):
                pass
    with archive_lease(tmp_path):
        pass


def test_collector_gaps_are_not_interpolated_and_wall_clock_jumps_do_not_change_tcx_time(tmp_path):
    from datetime import timedelta
    async def run():
        clock, session, store = await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        # Skip collector time but deliver fresh trainer telemetry afterwards.
        clock.start += timedelta(days=2)
        await pedaling(session,clock,4)
        await pedaling(session,clock,1)
        await session.command('stop')
        data = store.detail(session.recorder.session_id)
        assert data['summary']['active_duration_sec'] == 5
        assert data['summary']['coverage_power_w_sec'] == 2
        points = data['samples']
        for point in points:
            actual = datetime.fromisoformat(point['timestamp'])-datetime.fromisoformat(data['started_at'])
            assert actual.total_seconds() == point['offset_sec']
        assert 'trainer_received_at' in points[-1]
        assert data['events'][-1]['reason'] == 'manual_stop:commands_invalidated'
        await session.close()
    asyncio.run(run())


def test_gui_sessions_survive_browser_reload_and_server_restart(tmp_path):
    headers={'origin':'http://testserver'}
    with TestClient(create_app(data_dir=tmp_path)) as client:
        with client.websocket_connect('/ws/telemetry',headers=headers) as ws:
            while ws.receive_json().get('workout',{}).get('state') != 'READY':
                pass
            first=client.post('/api/workout',headers=headers,json={'action':'start'}).json()['session_id']
            assert first
            assert client.get('/api/sessions').json()[0]['status']=='ACTIVE'
            client.post('/api/workout',headers=headers,json={'action':'stop'})
            client.post('/api/workout',headers=headers,json={'action':'select','selected':'easy'})
            second=client.post('/api/workout',headers=headers,json={'action':'start'}).json()['session_id']
            assert second != first
        assert len(client.get('/api/sessions').json()) == 2
        # Graceful shutdown while riding finalizes the active session.
    with TestClient(create_app(data_dir=tmp_path)) as client:
        sessions=client.get('/api/sessions').json()
        assert {item['id'] for item in sessions} == {first,second}
        assert all(item['status']=='ABORTED' for item in sessions)
        exported=client.get(f'/api/sessions/{first}/download/json').json()
        assert exported['metadata']['simulated'] is True
        assert exported['events']
        assert exported['samples']


def test_positive_tcx_download_and_active_feedback_refusal(tmp_path):
    async def seed():
        clock,session,store=await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        await pedaling(session,clock,1)
        await session.command('stop')
        await session.close()
        return session.recorder.session_id
    identifier=asyncio.run(seed())
    with TestClient(create_app(data_dir=tmp_path)) as client:
        response=client.get(f'/api/sessions/{identifier}/download/tcx')
        assert response.status_code==200
        assert response.headers['content-type']=='application/vnd.garmin.tcx+xml'
        assert ET.fromstring(response.content).find('.//{*}Trackpoint') is not None
        store=SessionStore(tmp_path/'sessions.sqlite3')
        active=store.begin({'workout':{'name':'Active'},'simulated':True},datetime.now(timezone.utc).isoformat())
        assert client.post(f'/api/sessions/{active}/feedback',json={'rpe':5},headers={'origin':'http://testserver'}).status_code==409


def test_selecting_without_starting_cannot_relabel_completed_ride_on_shutdown(tmp_path):
    async def run():
        clock,session,store=await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        for _ in range(10):
            await pedaling(session,clock,1)
        identifier=session.recorder.session_id
        assert store.detail(identifier)['status']=='COMPLETED'
        # Hardware requires a new confirmed read-only connection for selection.
        await session.trainer.disconnect()
        await session.trainer.connect()
        await session.command('select','easy')
        assert session.recorder.session_id is None
        await session.close()
        assert store.detail(identifier)['status']=='COMPLETED'
        assert len(store.list_sessions())==1
    asyncio.run(run())


def test_cancelled_collector_joins_database_write_before_final_stop(tmp_path,monkeypatch):
    import threading
    async def run():
        clock,session,store=await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        entered,release=threading.Event(),threading.Event()
        original=store.append
        def slow(*args):
            entered.set()
            assert release.wait(3)
            original(*args)
        monkeypatch.setattr(store,'append',slow)
        task=asyncio.create_task(pedaling(session,clock,1))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await session.close()
        assert len(store.list_sessions())==1
        assert store.list_sessions()[0]['status']=='ABORTED'
    asyncio.run(run())


def test_trainer_loss_records_pause_reason_and_missing_power(tmp_path):
    async def run():
        clock,session,store=await owner(tmp_path)
        await pedaling(session,clock)
        await session.command('start')
        await pedaling(session,clock,1)
        session.trainer.lose_connection()
        await session.step()
        data=store.detail(session.recorder.session_id)
        assert session.engine.state=='PAUSED'
        assert any(event['reason']=='trainer_control_lost' for event in data['events'])
        assert data['samples'][-1]['power_w'] is None
        assert data['samples'][-1]['trainer_freshness']=='LOST'
        await session.command('stop')
        await session.close()
    asyncio.run(run())


def test_live_zero_is_a_measurement_not_a_missing_value(tmp_path):
    async def run():
        clock,session,store=await owner(tmp_path)
        session.trainer.emit_sample(power_w=0,cadence_rpm=82)
        session.hr.emit_sample(125)
        await session.step()
        await session.command('start')
        clock.advance(1)
        session.trainer.emit_sample(power_w=0,cadence_rpm=82)
        await session.step()
        await session.command('stop')
        data=store.detail(session.recorder.session_id)
        assert data['summary']['average_power_w']==0
        assert data['summary']['coverage_power_w_sec']==1
        assert b'<ae:Watts>0</ae:Watts>' in export_tcx(data)
        await session.close()
    asyncio.run(run())
