"""Opt-in simulator-only M2 execution. Never imports or opens a BLE backend."""
import argparse
import asyncio
from dataclasses import asdict
import json
import yaml
from pathlib import Path

from .engine import WorkoutEngine, WorkoutState
from .simulation import ManualClock, SimulatedHeartRateSource, SimulatedTrainer
from .workout import load_workout


def output(engine: WorkoutEngine) -> None:
    while not engine.events.empty():
        event = engine.events.get_nowait()
        engine.events.task_done()
        print(json.dumps({"simulated": True, **asdict(event)}))


async def run(path: Path, baseline_w: int | None, scenario: str) -> int:
    workout = load_workout(path)
    clock = ManualClock()
    trainer = SimulatedTrainer(clock=clock, command_timeout_sec=0.05)
    hr = SimulatedHeartRateSource(clock=clock)
    engine = WorkoutEngine(trainer, hr, workout, baseline_w=baseline_w, clock=clock)
    try:
        await engine.prepare()
        trainer.emit_sample()
        hr.emit_sample()
        trainer.faults.timeout = scenario == "timeout"
        trainer.faults.rejected_control = scenario == "rejected-control"
        await engine.start()
        output(engine)
        injected = False
        for step in range(workout.duration_sec + 60):
            clock.advance(1)
            if step == 3:
                injected = True
                if scenario == "disconnect":
                    trainer.lose_connection()
                elif scenario == "stale-hr":
                    hr.faults.silent_hr = True
                elif scenario == "cadence-collapse":
                    trainer.faults.cadence_collapse = True
            trainer.emit_sample()
            hr.emit_sample()
            await engine.tick()
            await asyncio.sleep(0)
            output(engine)
            if engine.state == WorkoutState.COMPLETED:
                return 0
            if engine.state == WorkoutState.PAUSED and injected:
                if scenario == "disconnect" and not engine.recovery_armed:
                    continue
                # The demonstration deliberately never supplies consent to resume.
                print(json.dumps({"simulated": True, "state": engine.state,
                                  "reason": "explicit_resume_required", "elapsed_sec": engine.elapsed_sec}))
                return 1
        return 1
    except (ValueError, RuntimeError, ConnectionError, TimeoutError) as exc:
        print(json.dumps({"simulated": True, "failure": type(exc).__name__, "detail": str(exc)}))
        return 1
    finally:
        await engine.close()
        output(engine)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workout", type=Path, help="validated YAML workout")
    parser.add_argument("--baseline-w", type=int, help="explicit indoor baseline for percentage targets")
    parser.add_argument("--scenario", choices=["normal", "disconnect", "stale-hr", "cadence-collapse",
                                             "timeout", "rejected-control"], default="normal")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.workout, args.baseline_w, args.scenario))
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f"Workout refused: {exc}\n")
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
