"""Independent BLE lifecycle orchestration using fake advertisements/transports."""
import asyncio
from contextlib import asynccontextmanager
import struct
from types import SimpleNamespace
import pytest
from kickrcoach import ble
from kickrcoach.cli import Config
from kickrcoach.domain import ConnectionState as CS
from kickrcoach.hardware_telemetry import HardwareTelemetry
from kickrcoach.protocol import FTMS,HRS,BIKE,HEART
from kickrcoach.simulation import MemoryTransport,Faults


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(),1)


@asynccontextmanager
async def environment(monkeypatch, available, *, fail=None, gate=None, config_kwargs=None):
    transports={'trainer':[],'hr':[]}
    scans=[]
    async def discover(_timeout):
        scans.append(set(available))
        return {role:(SimpleNamespace(address=role+'-fixture'),
                      SimpleNamespace(service_uuids=[FTMS if role=='trainer' else HRS]))
                for role in available}
    def factory(device):
        role=device.address.split('-')[0]
        def make(callback):
            transport=MemoryTransport(role,callback,Faults())
            original=transport.connect
            async def connect():
                if role not in available or (fail and fail.get(role,False)):
                    raise ConnectionError('fixture device temporarily unavailable')
                if gate and role in gate:
                    await gate[role].wait()
                await original()
            transport.connect=connect
            transports[role].append(transport)
            return transport
        return make
    monkeypatch.setattr(ble,'discover',discover)
    monkeypatch.setattr(ble,'transport_factory',factory)
    service=HardwareTelemetry(Config(hardware=True,trainer_id='trainer-fixture',hr_id='hr-fixture',**(config_kwargs or {})),retry_sec=.01)
    task=asyncio.create_task(service.collect())
    try:
        yield service,transports,scans
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        assert all(not t.is_connected and not t.writes for values in transports.values() for t in values)


def emit(transports,role):
    transport=transports[role][-1]
    if role=='trainer':
        transport.notify(BIKE,struct.pack('<HHHh',0x44,2000,160,42))
    else:
        transport.notify(HEART,struct.pack('<BH',1,120))


@pytest.mark.parametrize('first',['trainer','hr'])
def test_sensors_can_appear_in_either_order_without_waiting_for_each_other(monkeypatch,first):
    async def run():
        available={first}
        other='hr' if first=='trainer' else 'trainer'
        async with environment(monkeypatch,available) as (service,transports,scans):
            source=service.trainer if first=='trainer' else service.hr
            await until(lambda:source.connection==CS.CONNECTED)
            emit(transports,first)
            await until(lambda:service.snapshot()[first].get('power_w' if first=='trainer' else 'bpm') is not None)
            assert service.snapshot()[other]['connection']==CS.DISCONNECTED
            original=transports[first][-1]
            available.add(other)
            await until(lambda:service.trainer.connection==CS.CONNECTED and service.hr.connection==CS.CONNECTED)
            emit(transports,other)
            await until(lambda:service.snapshot()[other].get('power_w' if other=='trainer' else 'bpm') is not None)
            assert transports[first][-1] is original and original.is_connected
    asyncio.run(run())


def test_both_devices_can_be_off_at_start_then_appear_later(monkeypatch):
    async def run():
        available=set()
        async with environment(monkeypatch,available) as (service,transports,scans):
            await until(lambda:len(scans)>=2)
            assert service.snapshot()['trainer']['power_w'] is None
            assert service.snapshot()['hr']['bpm'] is None
            available.update(['trainer','hr'])
            await until(lambda:service.trainer.connection==CS.CONNECTED and service.hr.connection==CS.CONNECTED)
            assert service.error is None
    asyncio.run(run())


@pytest.mark.parametrize('lost',['trainer','hr'])
def test_disconnect_retries_only_affected_sensor_and_clears_stale_value(monkeypatch,lost):
    async def run():
        available={'trainer','hr'}
        other='hr' if lost=='trainer' else 'trainer'
        async with environment(monkeypatch,available) as (service,transports,scans):
            await until(lambda:service.trainer.connection==CS.CONNECTED and service.hr.connection==CS.CONNECTED)
            for role in available:
                emit(transports,role)
            await until(lambda:service.snapshot()['trainer']['power_w']==42 and service.hr.latest.bpm==120)
            original_other=transports[other][-1]
            available.remove(lost)
            transports[lost][-1].lose_connection()
            await until(lambda:len(transports[lost])>=2)
            assert service.snapshot()[lost]['power_w' if lost=='trainer' else 'bpm'] is None
            assert transports[other][-1] is original_other and original_other.is_connected
            available.add(lost)
            await until(lambda:service.trainer.connection==CS.CONNECTED and service.hr.connection==CS.CONNECTED)
            assert service.snapshot()[lost]['power_w' if lost=='trainer' else 'bpm'] is None  # No cached current value.
            emit(transports,lost)
            await until(lambda:service.snapshot()[lost]['power_w' if lost=='trainer' else 'bpm'] is not None)
            assert transports[other][-1] is original_other
    asyncio.run(run())


def test_slow_hr_setup_does_not_block_trainer_values(monkeypatch):
    async def run():
        gate={'hr':asyncio.Event()}
        async with environment(monkeypatch,{'trainer','hr'},gate=gate) as (service,transports,scans):
            await until(lambda:service.trainer.connection==CS.CONNECTED and service.hr.connection==CS.CONNECTING)
            emit(transports,'trainer')
            await until(lambda:service.snapshot()['trainer']['power_w']==42)
            gate['hr'].set()
            await until(lambda:service.hr.connection==CS.CONNECTED)
    asyncio.run(run())


def test_one_setup_failure_is_retried_while_other_sensor_remains_usable(monkeypatch):
    async def run():
        fail={'hr':True}
        async with environment(monkeypatch,{'trainer','hr'},fail=fail) as (service,transports,scans):
            await until(lambda:service.trainer.connection==CS.CONNECTED and len(transports['hr'])>=2)
            emit(transports,'trainer')
            await until(lambda:service.snapshot()['trainer']['power_w']==42)
            original=transports['trainer'][-1]
            fail['hr']=False
            await until(lambda:service.hr.connection==CS.CONNECTED)
            assert transports['trainer'][-1] is original
    asyncio.run(run())


def test_silent_hr_connection_is_reestablished_without_touching_trainer(monkeypatch):
    async def run():
        async with environment(monkeypatch,{'trainer','hr'},config_kwargs={'stale_sec':.01,'lost_sec':.04}) as (service,transports,scans):
            await until(lambda:service.hr.connection==CS.CONNECTED and service.trainer.connection==CS.CONNECTED)
            emit(transports,'hr')
            await until(lambda:service.hr.latest.bpm==120)
            original=transports['trainer'][-1]
            await until(lambda:len(transports['hr'])>=2 and service.hr.connection==CS.CONNECTED)
            assert service.hr.latest.bpm is None
            assert transports['trainer'][-1] is original and original.is_connected
    asyncio.run(run())


def test_transient_scanner_failure_is_not_fatal(monkeypatch):
    async def run():
        async with environment(monkeypatch,{'trainer'}) as (service,transports,scans):
            original=ble.discover
            attempts=[]
            async def faulty(timeout):
                attempts.append(1)
                if len(attempts)==1:
                    raise OSError('Bluetooth temporarily unavailable')
                return await original(timeout)
            monkeypatch.setattr(ble,'discover',faulty)
            await until(lambda:service.trainer.connection==CS.CONNECTED)
            assert len(attempts)>=2
            emit(transports,'trainer')
            await until(lambda:service.snapshot()['trainer']['power_w']==42)
            assert service.snapshot()['hr']['bpm'] is None
    asyncio.run(run())
