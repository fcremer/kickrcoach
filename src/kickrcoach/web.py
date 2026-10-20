"""Local, read-only telemetry dashboard. No trainer-control endpoints."""
import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import logging
import math
from pathlib import Path
import tomllib
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ble
from .cli import Config
from .domain import ConnectionState, HeartRateSource, SystemClock, TrainerAdapter, TrainerSample
from .heart_rate import BleHeartRateSource
from .protocol import FTMS, HRS
from .simulation import SimulatedHeartRateSource, SimulatedTrainer
from .trainer import FtmsTrainer

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")


class Telemetry:
    """One collector for all browsers; bounded ephemeral history, no recording."""
    def __init__(self, trainer: TrainerAdapter, hr: HeartRateSource, *, simulated: bool,
                 stale_sec: float = 5, lost_sec: float = 15, clock: SystemClock | None = None):
        self.trainer, self.hr, self.simulated = trainer, hr, simulated
        self.clock = clock or SystemClock()
        self.stale_sec, self.lost_sec = stale_sec, lost_sec
        self.started = self.clock.monotonic()
        self.sample: TrainerSample | None = None
        self.history: deque[dict] = deque(maxlen=180)
        self.error: str | None = None
        self.sequence = 0

    def snapshot(self) -> dict:
        now = self.clock.monotonic()
        age = None if self.sample is None else max(0, now - self.sample.received.monotonic)
        connected = self.trainer.connection == ConnectionState.CONNECTED
        freshness = ("LOST" if not connected or age is None or age >= self.lost_sec else
                     "STALE" if age >= self.stale_sec else "LIVE")
        live = self.sample if freshness == "LIVE" else None
        hr = self.hr.latest
        return {"simulated": self.simulated, "sequence": self.sequence,
                "elapsed_sec": max(0, now - self.started), "error": self.error,
                "trainer": {"connection": self.trainer.connection, "freshness": freshness,
                            "age_sec": age, "power_w": live.power_w if live else None,
                            "cadence_rpm": live.cadence_rpm if live else None},
                "hr": {"connection": self.hr.connection, "freshness": hr.freshness,
                       "bpm": hr.bpm,
                       "age_sec": max(0, now - hr.received.monotonic) if hr.received else None}}

    async def collect(self) -> None:
        # Dedicated consumers prevent competing browser clients from stealing events.
        async def consume(source: TrainerAdapter | HeartRateSource) -> None:
            while True:
                event = await source.events.get()
                if isinstance(event, TrainerSample):
                    self.sample = event
                source.events.task_done()

        consumers = [asyncio.create_task(consume(source)) for source in (self.trainer, self.hr)]
        try:
            results = await asyncio.gather(self.trainer.connect(), self.hr.connect(),
                                           return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                self.error = "Geräteverbindung fehlgeschlagen. Bluetooth, Broadcast und Geräteauswahl prüfen; App neu starten."
                log.error("Telemetry device setup failed (details omitted to protect device identifiers)")
            step = 0
            while True:
                if self.simulated:
                    assert isinstance(self.trainer, SimulatedTrainer)
                    assert isinstance(self.hr, SimulatedHeartRateSource)
                    self.trainer.emit_sample(power_w=round(125 + 18 * math.sin(step / 9)),
                                             cadence_rpm=82 + 4 * math.sin(step / 7))
                    self.hr.emit_sample(round(118 + 6 * math.sin(step / 15)))
                await asyncio.sleep(1)
                self.sequence += 1
                point = self.snapshot()
                self.history.append({"t": point["elapsed_sec"],
                                     "power": point["trainer"]["power_w"],
                                     "cadence": point["trainer"]["cadence_rpm"],
                                     "hr": point["hr"]["bpm"]})
                step += 1
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            results = await asyncio.gather(self.trainer.disconnect(), self.hr.disconnect(),
                                           return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                log.error("Device cleanup unconfirmed; check Bluetooth connections")


def create_app(config: Config | None = None, telemetry: Telemetry | None = None) -> FastAPI:
    config = config or Config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        try:
            if telemetry is not None:
                app.state.telemetry = telemetry
            elif config.hardware:
                # Server remains available while macOS scans/connects.
                app.state.telemetry = None
            else:
                app.state.telemetry = Telemetry(SimulatedTrainer(), SimulatedHeartRateSource(), simulated=True)

            async def run() -> None:
                try:
                    if config.hardware and telemetry is None:
                        found = await ble.discover(config.scan_sec)
                        trainer = FtmsTrainer(ble.transport_factory(ble.select(found, config.trainer_id, FTMS)))
                        hr = BleHeartRateSource(ble.transport_factory(ble.select(found, config.hr_id, HRS)),
                                               stale_sec=config.stale_sec, lost_sec=config.lost_sec)
                        app.state.telemetry = Telemetry(trainer, hr, simulated=False,
                                                        stale_sec=config.stale_sec, lost_sec=config.lost_sec)
                    await app.state.telemetry.collect()
                except Exception:
                    app.state.failure = "Bluetooth-Verbindung fehlgeschlagen. Geräte einschalten, IDs und Berechtigung prüfen; App neu starten."
                    log.error("Telemetry service failed (device details omitted)")

            app.state.failure = None
            task = asyncio.create_task(run())
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="KICKR Coach · Live", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws/telemetry")
    async def stream(ws: WebSocket) -> None:
        if ws.headers.get("origin") not in (f"http://{ws.headers.get('host')}", f"https://{ws.headers.get('host')}"):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                source = app.state.telemetry
                if source is None:
                    payload = {"status": "connecting", "simulated": not config.hardware}
                else:
                    payload = {"status": "ready", **source.snapshot(), "history": list(source.history)}
                payload["error"] = app.state.failure or payload.get("error")
                await ws.send_json(payload)
                await asyncio.sleep(1)
        except (WebSocketDisconnect, OSError):
            pass

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true", help="Connect read-only to configured BLE devices")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        values = tomllib.loads(args.config.read_text()) if args.config else {}
        if set(values) - set(Config.__dataclass_fields__) or {"hardware", "control_test", "scenario"} & set(values):
            raise ValueError("Unsupported dashboard config keys")
        config = Config(**values, hardware=args.hardware)
        config.validate()
        if args.hardware and (not config.trainer_id.strip() or not config.hr_id.strip() or
                              config.trainer_id == config.hr_id):
            raise ValueError("Hardware requires two distinct trainer_id/hr_id identifiers")
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be within 1..65535")
    except (ValueError, TypeError, OSError):
        parser.error("Konfiguration ungültig: TOML, trainer_id/hr_id und Port prüfen.")
    import uvicorn
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
