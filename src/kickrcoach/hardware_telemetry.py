"""Independent, retrying read-only BLE sessions for the web dashboard."""
import asyncio
import logging
import math
from . import ble
from .cli import Config
from .domain import ConnectionState as CS, Freshness
from .heart_rate import BleHeartRateSource
from .protocol import FTMS, HRS
from .telemetry import Telemetry
from .trainer import FtmsTrainer
from .transport import Lost, GattTransport

log = logging.getLogger(__name__)


class HardwareTelemetry(Telemetry):
    def __init__(self, config: Config, *, retry_sec: float = 3):
        if not math.isfinite(retry_sec) or retry_sec <= 0:
            raise ValueError("retry interval must be finite and positive")
        self.config = config
        self.retry_sec = retry_sec
        self._devices: dict[str, object] = {}
        self._connected_at: dict[str, float] = {}
        self._wake = {role:asyncio.Event() for role in ('trainer','hr')}
        self._detail = {role:'Wird gesucht …' for role in ('trainer','hr')}

        def trainer_factory(callback: Lost) -> GattTransport:
            return ble.transport_factory(self._devices['trainer'])(callback)

        def hr_factory(callback: Lost) -> GattTransport:
            return ble.transport_factory(self._devices['hr'])(callback)

        trainer = FtmsTrainer(trainer_factory, command_timeout_sec=config.command_timeout_sec)
        hr = BleHeartRateSource(hr_factory, stale_sec=config.stale_sec, lost_sec=config.lost_sec)
        super().__init__(trainer, hr, simulated=False, stale_sec=config.stale_sec, lost_sec=config.lost_sec)

    def snapshot(self) -> dict:
        payload = super().snapshot()
        for role, source in (('trainer',self.trainer),('hr',self.hr)):
            payload[role]['detail'] = 'Verbunden' if source.connection == CS.CONNECTED else self._detail[role]
        return payload

    async def _scan(self) -> None:
        while True:
            missing = [role for role, source in (('trainer',self.trainer),('hr',self.hr))
                       if source.connection not in (CS.CONNECTED, CS.CONNECTING)]
            if missing:
                try:
                    found = await ble.discover(self.config.scan_sec)
                    for role, identifier, service in (('trainer',self.config.trainer_id,FTMS),
                                                       ('hr',self.config.hr_id,HRS)):
                        if role not in missing:
                            continue
                        try:
                            device = ble.select(found, identifier, service)
                        except ValueError:
                            # No candidate is not a fatal error. A connected other
                            # sensor remains untouched while we scan again.
                            continue
                        self._devices[role] = device
                        self._wake[role].set()
                except Exception:
                    for role in missing:
                        self._detail[role] = 'Bluetooth prüfen · suche erneut'
                    log.warning('BLE discovery failed; retrying (device details omitted)')
            await asyncio.sleep(self.retry_sec)

    async def _connect(self, role: str) -> None:
        source = self.trainer if role == 'trainer' else self.hr
        while True:
            if source.connection == CS.CONNECTED:
                silent_hr = (role == 'hr' and self.hr.latest.freshness == Freshness.LOST
                             and self.clock.monotonic() - self._connected_at.get(role,self.clock.monotonic())
                             >= self.config.lost_sec)
                if not silent_hr:
                    await asyncio.sleep(min(0.25,self.retry_sec))
                    continue
            if role == 'trainer':
                self.sample = None
            self._detail[role] = 'Wird gesucht …'
            if role not in self._devices:
                self._wake[role].clear()
                try:
                    await asyncio.wait_for(self._wake[role].wait(), timeout=self.retry_sec)
                except TimeoutError:
                    pass
                continue
            self._detail[role] = 'Verbindet …'
            try:
                # Cleanup only this sensor. There has never been a control request,
                # so FtmsTrainer.disconnect sends no FTMS Stop/target command.
                await source.disconnect()
                await source.connect()
                self._detail[role] = 'Verbunden'
                self._connected_at[role] = self.clock.monotonic()
            except Exception:
                self._detail[role] = 'Warte auf Gerät · versuche erneut'
                self._devices.pop(role, None)
                log.warning('%s connection failed; retrying (device details omitted)',role)
                try:
                    await source.disconnect()
                except Exception:
                    log.warning('%s cleanup unconfirmed; retrying',role)
                await asyncio.sleep(self.retry_sec)

    async def collect(self) -> None:
        tasks = [asyncio.create_task(self._scan()),
                 asyncio.create_task(self._connect('trainer')),
                 asyncio.create_task(self._connect('hr')),
                 asyncio.create_task(self.consume(self.trainer)),
                 asyncio.create_task(self.consume(self.hr))]
        try:
            while True:
                self.sequence += 1
                point = self.snapshot()
                self.history.append({'t':point['elapsed_sec'],'power':point['trainer']['power_w'],
                                     'cadence':point['trainer']['cadence_rpm'],'hr':point['hr']['bpm']})
                await asyncio.sleep(1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            results = await asyncio.gather(self.trainer.disconnect(),self.hr.disconnect(),return_exceptions=True)
            if any(isinstance(result,BaseException) for result in results):
                log.error('Device cleanup unconfirmed; check Bluetooth connections')
