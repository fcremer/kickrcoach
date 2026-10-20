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
