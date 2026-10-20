"""Read-only dashboard contracts; never requires Bluetooth hardware."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from kickrcoach.domain import ReceivedAt, TrainerSample
from kickrcoach.simulation import ManualClock, SimulatedTrainer, SimulatedHeartRateSource
from kickrcoach.web import Telemetry, create_app


def test_trainer_and_hr_values_expire_with_monotonic_time():
    async def run():
        clock = ManualClock()
        trainer = SimulatedTrainer(clock=clock)
        hr = SimulatedHeartRateSource(clock=clock)
        service = Telemetry(trainer, hr, simulated=True, clock=clock)
        await trainer.connect()
        await hr.connect()
        try:
            service.sample = TrainerSample(ReceivedAt.now(clock), power_w=123, cadence_rpm=82)
            hr.emit_sample(120)
            assert service.snapshot()['trainer']['power_w'] == 123
            assert service.snapshot()['hr']['bpm'] == 120
            clock.advance(5)
            assert service.snapshot()['trainer']['freshness'] == 'STALE'
            assert service.snapshot()['trainer']['power_w'] is None
            assert service.snapshot()['hr']['bpm'] is None
            clock.advance(10)
            assert service.snapshot()['trainer']['freshness'] == 'LOST'
            assert service.snapshot()['hr']['freshness'] == 'LOST'
            assert service.snapshot()['elapsed_sec'] == 15
        finally:
            await trainer.disconnect()
            await hr.disconnect()
    asyncio.run(run())


def test_missing_fields_and_disconnect_never_carry_old_values():
    async def run():
        clock = ManualClock()
        trainer = SimulatedTrainer(clock=clock)
        hr = SimulatedHeartRateSource(clock=clock)
        service = Telemetry(trainer, hr, simulated=True, clock=clock)
        await trainer.connect()
        try:
            service.sample = TrainerSample(ReceivedAt.now(clock), power_w=0)
            assert service.snapshot()['trainer']['power_w'] == 0
            assert service.snapshot()['trainer']['cadence_rpm'] is None
            trainer.lose_connection()
            assert service.snapshot()['trainer']['power_w'] is None
        finally:
            await trainer.disconnect()
    asyncio.run(run())


def test_browser_clients_share_readonly_session_and_shutdown_writes_nothing():
    trainer = SimulatedTrainer()
    hr = SimulatedHeartRateSource()
    service = Telemetry(trainer, hr, simulated=True)
    with TestClient(create_app(telemetry=service)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/static/style.css').status_code == 200
        with client.websocket_connect('/ws/telemetry', headers={'origin':'http://testserver'}) as first:
            first.receive_json()
            data = first.receive_json()
            assert data['status'] == 'ready'
            assert data['simulated'] is True
            assert data['trainer']['power_w'] is not None
            with client.websocket_connect('/ws/telemetry', headers={'origin':'http://testserver'}) as second:
                assert second.receive_json()['elapsed_sec'] >= data['elapsed_sec']
        # Reopening a browser doesn't replace the BLE transport or request control.
        transport = trainer.transport
        with client.websocket_connect('/ws/telemetry', headers={'origin':'http://testserver'}) as third:
            assert third.receive_json()['status'] == 'ready'
        assert trainer.transport is transport
        assert trainer.transport.writes == []
        assert client.post('/api/target', json={'watts':50}).status_code == 404
        assert client.get('/', headers={'host':'evil.example'}).status_code == 400
    assert trainer.transport.writes == []
    assert not trainer.transport.is_connected
    assert not hr.transport.is_connected


@pytest.mark.parametrize('origin', ['https://evil.example', None])
def test_websocket_rejects_cross_origin_and_missing_origin(origin):
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app()) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/telemetry', headers={'origin':origin} if origin else {}):
                pass


def test_failed_device_setup_still_serves_dashboard():
    trainer = SimulatedTrainer()
    hr = SimulatedHeartRateSource()
    async def fail():
        raise OSError('private device identifier must not reach browser')
    trainer.connect = fail
    service = Telemetry(trainer, hr, simulated=False)
    with TestClient(create_app(telemetry=service)) as client:
        with client.websocket_connect('/ws/telemetry', headers={'origin':'http://testserver'}) as ws:
            ws.receive_json()
            data = ws.receive_json()
            assert 'fehlgeschlagen' in data['error']
            assert 'private device' not in str(data)
            assert data['trainer']['power_w'] is None
            assert data['hr']['connection'] == 'CONNECTED'


def test_history_is_bounded():
    service = Telemetry(SimulatedTrainer(), SimulatedHeartRateSource(), simulated=True)
    for n in range(300):
        service.history.append({'t':n})
    assert len(service.history) == 180
    assert service.history[0]['t'] == 120


def test_hardware_route_uses_selected_transports_without_control(monkeypatch):
    from kickrcoach import web
    from kickrcoach.cli import Config
    from kickrcoach.protocol import FTMS, HRS
    from kickrcoach.simulation import Faults, MemoryTransport
    transports = []
    async def discover(_timeout):
        return {'fixture': 'only fake devices'}
    def select(_found, identifier, service):
        assert (identifier, service) in [('trainer-fixture', FTMS), ('hr-fixture', HRS)]
        return 'trainer' if service == FTMS else 'hr'
    def factory(role):
        def make(callback):
            transport = MemoryTransport(role, callback, Faults())
            transports.append(transport)
            return transport
        return make
    monkeypatch.setattr(web.ble, 'discover', discover)
    monkeypatch.setattr(web.ble, 'select', select)
    monkeypatch.setattr(web.ble, 'transport_factory', factory)
    config = Config(hardware=True, trainer_id='trainer-fixture', hr_id='hr-fixture')
    with TestClient(create_app(config)) as client:
        with client.websocket_connect('/ws/telemetry', headers={'origin':'http://testserver'}) as ws:
            ws.receive_json()
            payload = ws.receive_json()
            assert payload['simulated'] is False
            assert payload['trainer']['connection'] == 'CONNECTED'
            assert payload['trainer']['power_w'] is None  # No fake hardware readings.
            assert payload['hr']['bpm'] is None
    assert len(transports) == 2
    assert all(transport.writes == [] and not transport.is_connected for transport in transports)


def test_simulator_workout_controls_and_reload_keep_same_session():
    from kickrcoach.cli import Config
    config = Config(hr_zone_boundaries=[100,120,140,160])
    headers = {'origin':'http://testserver'}
    with TestClient(create_app(config)) as client:
        with client.websocket_connect('/ws/telemetry', headers=headers) as ws:
            ws.receive_json()
            payload = ws.receive_json()
            assert payload['hr_zone_boundaries'] == [100,120,140,160]
            assert payload['workout']['state'] == 'READY'
            assert payload['workout']['target_w'] is None
            def command(action, **extra):
                response = client.post('/api/workout',headers=headers,json={'action':action,**extra})
                assert response.status_code == 200, response.text
                return response.json()
            assert command('start')['state'] == 'RUNNING'
            assert command('increase')['target_w'] == 45
            assert command('pause')['state'] == 'PAUSED'
            assert client.post('/api/workout',headers=headers,json={'action':'select','selected':'easy'}).status_code == 409
            assert command('resume')['state'] == 'RUNNING'
            with client.websocket_connect('/ws/telemetry', headers=headers) as another:
                second = another.receive_json()
                assert second['workout']['state'] == 'RUNNING'
                assert second['workout']['target_w'] == 45
            assert command('stop')['state'] == 'ABORTED'
            assert client.post('/api/workout',headers=headers,json={'action':'resume'}).status_code == 409
            selected = command('select',selected='easy')
            assert selected['state'] == 'READY' and selected['selected'] == 'easy'
            assert selected['remaining_sec'] == 120
            assert command('start')['target_w'] == 50
            assert command('decrease')['target_w'] == 45
            command('stop')


@pytest.mark.parametrize('origin', [None, 'https://evil.example'])
def test_workout_commands_require_same_origin(origin):
    with TestClient(create_app()) as client:
        response=client.post('/api/workout',headers={'origin':origin} if origin else {},json={'action':'start'})
        assert response.status_code == 403


def test_hardware_and_readonly_collectors_cannot_receive_workout_commands():
    trainer=SimulatedTrainer()
    hr=SimulatedHeartRateSource()
    service=Telemetry(trainer,hr,simulated=False)
    with TestClient(create_app(telemetry=service)) as client:
        response=client.post('/api/workout',headers={'origin':'http://testserver'},json={'action':'start'})
        assert response.status_code == 403
    assert trainer.transport.writes == []


def test_invalid_workout_command_is_rejected_before_dispatch():
    with TestClient(create_app()) as client:
        assert client.post('/api/workout',headers={'origin':'http://testserver'},json={'action':'set_power','watts':999}).status_code == 422


def test_stop_during_block_transition_does_not_cancel_telemetry_owner():
    from kickrcoach.web_workout import WebWorkout
    from kickrcoach.engine import WorkoutState
    async def run():
        session=WebWorkout()
        try:
            await session.prepare()
            await session.command('start')
            session.engine._since-=60  # Due block transition without real waiting.
            entered=asyncio.Event()
            original=session.trainer.transport.write_gatt_char
            async def pending(characteristic,data,response=True):
                if data[0]==5:
                    entered.set()
                    await asyncio.Event().wait()
                await original(characteristic,data,response)
            session.trainer.transport.write_gatt_char=pending
            collector=asyncio.create_task(session.step())
            await entered.wait()
            await session.command('stop')
            await asyncio.wait_for(collector,1)
            assert not collector.cancelled()
            assert session.engine.state==WorkoutState.ABORTED
            await session.step()  # Terminal workout still allows live telemetry.
            # A cancelled protocol procedure may leave the adapter uncertain;
            # keep collecting, but never present its cached value as current.
            view=Telemetry(session.trainer,session.hr,simulated=True,workout=session)
            view.sample=session.sample
            assert view.snapshot()['trainer']['power_w'] is None
            assert session.trainer.target_power_w is None
            await session.command('select',selected='easy')
            await session.step()
            assert session.sample.power_w==0
        finally:
            await session.close()
    asyncio.run(run())
