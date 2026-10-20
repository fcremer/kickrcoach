"""Local dashboard with optional, explicitly enabled hardware workout control."""
import argparse
import asyncio
from dataclasses import asdict
from contextlib import asynccontextmanager
import logging
import json
import os
from pathlib import Path
import tomllib
from typing import AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ble
from .telemetry import Telemetry
from .hardware_telemetry import HardwareTelemetry
from .cli import Config
from .web_workout import WebWorkout
from .recording import SessionStore, SessionRecorder, export_tcx, archive_lease

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")



class WorkoutCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["select", "start", "pause", "resume", "increase", "decrease", "stop"]
    selected: str | None = None


class SessionFeedback(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rpe: int | None = Field(default=None, strict=True, ge=0, le=10)
    cooling: bool | None = Field(default=None, strict=True)
    notes: str | None = Field(default=None, max_length=4000)


def create_app(config: Config | None = None, telemetry: Telemetry | None = None, *, enable_control: bool = False,
               data_dir: Path | None = None) -> FastAPI:
    config = config or Config()
    config.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        archive = data_dir or Path(os.environ.get('KICKRCOACH_DATA_DIR',
            str(Path.home() / 'Library' / 'Application Support' / 'KICKR Coach')))
        with archive_lease(archive):
            app.state.store = await asyncio.to_thread(SessionStore, archive / 'sessions.sqlite3')
            try:
                if telemetry is not None:
                    app.state.telemetry = telemetry
                elif config.hardware:
                    # Server remains available while macOS scans/connects.
                    app.state.telemetry = None
                else:
                    session = WebWorkout(maximum_w=config.training_maximum_w,
                                         stale_sec=config.stale_sec,lost_sec=config.lost_sec)
                    app.state.telemetry = Telemetry(session.trainer, session.hr, simulated=True, workout=session)

                async def run() -> None:
                    try:
                        if config.hardware and telemetry is None:
                            app.state.telemetry = HardwareTelemetry(config,control_enabled=enable_control)
                        owner = app.state.telemetry.workout
                        if owner is not None:
                            owner.recorder = SessionRecorder(app.state.store,
                                simulated=app.state.telemetry.simulated,
                                context={'hr_zone_boundaries':list(config.hr_zone_boundaries),
                                         'training_maximum_w':config.training_maximum_w,
                                         'baseline_w':owner.engine.baseline_w,
                                         'safety_config':asdict(owner.engine.config),
                                         'stale_sec':owner.stale_sec, 'lost_sec':owner.lost_sec,
                                         'readiness':None, 'rpe':None, 'cooling':None, 'notes':None})
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

    @app.middleware('http')
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/workout")
    async def command(body: WorkoutCommand, request: Request) -> dict:
        if request.headers.get("origin") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Zugriff nur aus der lokalen Webansicht.")
        source = app.state.telemetry
        if source is None or source.workout is None or (not source.simulated and not enable_control):
            raise HTTPException(403, "Echte Trainingssteuerung muss beim Serverstart ausdrücklich freigegeben werden.")
        try:
            await source.workout.command(body.action, body.selected)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return source.workout.snapshot()

    @app.get('/api/sessions')
    async def sessions() -> list[dict]:
        return await asyncio.to_thread(app.state.store.list_sessions)

    async def session_detail(identifier: str) -> dict:
        try:
            return await asyncio.to_thread(app.state.store.detail, identifier)
        except KeyError as exc:
            raise HTTPException(404, 'Training nicht gefunden.') from exc

    @app.get('/api/sessions/{identifier}')
    async def detail(identifier: str) -> dict:
        session = await session_detail(identifier)
        return {key:value for key,value in session.items() if key not in ('samples','events')}

    @app.post('/api/sessions/{identifier}/feedback')
    async def feedback(identifier: str, body: SessionFeedback, request: Request) -> dict:
        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Zugriff nur aus der lokalen Webansicht.')
        try:
            await asyncio.to_thread(app.state.store.feedback, identifier, body.model_dump())
        except KeyError as exc:
            raise HTTPException(404, 'Training nicht gefunden.') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await session_detail(identifier)

    @app.get('/api/sessions/{identifier}/download/{format}')
    async def download(identifier: str, format: Literal['tcx', 'json']) -> Response:
        session = await session_detail(identifier)
        if format == 'tcx':
            try:
                content = await asyncio.to_thread(export_tcx, session)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            media_type = 'application/vnd.garmin.tcx+xml'
        else:
            content = (await asyncio.to_thread(json.dumps,session,ensure_ascii=False,indent=2)).encode('utf-8')
            media_type = 'application/json'
        # IDs come from the database, never from an interpolated path/header.
        return Response(content, media_type=media_type, headers={
            'Content-Disposition':f'attachment; filename="kickrcoach-{session["id"]}.{format}"',
            'Cache-Control':'no-store'})

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
    parser.add_argument("--hardware", action="store_true", help="Connect to configured BLE devices (read-only unless --enable-control)")
    parser.add_argument("--enable-control", action="store_true", help="Enable hardware GUI workout commands without an interactive prompt")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path, help="Local session archive directory")
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
    if args.enable_control:
        if not args.hardware:
            parser.error("--enable-control requires --hardware")
        print(f"GUI-Steuerung freigegeben: Starts bis {min(50,config.training_maximum_w)} W, "
              f"Trainingsgrenze {config.training_maximum_w} W. Start erfolgt in der GUI.")
    import uvicorn
    from copy import deepcopy
    from uvicorn.config import LOGGING_CONFIG
    log_config = deepcopy(LOGGING_CONFIG)
    log_config['loggers']['kickrcoach'] = {'handlers':['default'], 'level':'INFO', 'propagate':False}
    uvicorn.run(create_app(config,enable_control=args.enable_control, **({"data_dir":args.data_dir} if args.data_dir else {})), host="127.0.0.1", port=args.port,
                access_log=False, log_config=log_config)


if __name__ == "__main__":
    main()
