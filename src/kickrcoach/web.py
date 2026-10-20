"""Local telemetry dashboard; workout control is restricted to simulation."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import tomllib
from typing import AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ble
from .telemetry import Telemetry
from .hardware_telemetry import HardwareTelemetry
from .cli import Config
from .web_workout import WebWorkout

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")



class WorkoutCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["select", "start", "pause", "resume", "increase", "decrease", "stop"]
    selected: str | None = None


def create_app(config: Config | None = None, telemetry: Telemetry | None = None) -> FastAPI:
    config = config or Config()
    config.validate()

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
                session = WebWorkout()
                app.state.telemetry = Telemetry(session.trainer, session.hr, simulated=True, workout=session)

            async def run() -> None:
                try:
                    if config.hardware and telemetry is None:
                        app.state.telemetry = HardwareTelemetry(config)
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

    @app.post("/api/workout")
    async def command(body: WorkoutCommand, request: Request) -> dict:
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Zugriff nur aus der lokalen Webansicht.")
        source = app.state.telemetry
        if config.hardware or source is None or source.workout is None or not source.simulated:
            raise HTTPException(403, "Workout-Steuerung ist ausschließlich im Simulator verfügbar.")
        try:
            await source.workout.command(body.action, body.selected)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return source.workout.snapshot()

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
                payload["hr_zone_boundaries"] = list(config.hr_zone_boundaries)
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
        parser.error("Konfiguration ungültig: TOML, trainer_id/hr_id, hr_zone_boundaries und Port prüfen.")
    import uvicorn
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
