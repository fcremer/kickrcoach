"""Fast, deterministic M1 adapter demo. Simulation only; never opens Bluetooth."""
import argparse
import asyncio
from dataclasses import asdict
import json
from .domain import TrainerAdapter, HeartRateSource
from .simulation import Faults, ManualClock, SimulatedHeartRateSource, SimulatedTrainer


def output(source: TrainerAdapter | HeartRateSource, role: str) -> None:
    while not source.events.empty():
        event = source.events.get_nowait()
        source.events.task_done()
        print(json.dumps({"simulated": True, "role": role, "type": type(event).__name__,
                          **asdict(event)}, default=str))


async def run(steps: int, scenario: str) -> int:
    clock = ManualClock()
    trainer = SimulatedTrainer(clock=clock, command_timeout_sec=.05)
    hr = SimulatedHeartRateSource(clock=clock)
    try:
        await asyncio.gather(trainer.connect(), hr.connect())
        trainer.faults.timeout = scenario == "timeout"
        trainer.faults.rejected_control = scenario == "rejected-control"
        await trainer.request_control()
        await trainer.set_target_power(50)
        await trainer.resume()
        for step in range(steps):
            if step == 3:
                if scenario == "disconnect":
                    trainer.lose_connection()
                    raise ConnectionError("simulated trainer disconnect; no auto-reconnect")
                trainer.faults.cadence_collapse = scenario == "cadence-collapse"
                hr.faults.silent_hr = scenario == "stale-hr"
            trainer.emit_sample()
            hr.emit_sample()
            clock.advance(1)
            hr.refresh()
            output(trainer, "trainer")
            output(hr, "hr")
        return 0
    except Exception as exc:
        print(json.dumps({"simulated": True, "event": "demo_failure", "reason": str(exc)}))
        return 1
    finally:
        await asyncio.gather(trainer.disconnect(), hr.disconnect())
        output(trainer, "trainer")
        output(hr, "hr")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, default=20, help="virtual one-second steps (1..3600)")
    parser.add_argument("--scenario", default="normal", choices=["normal", "timeout", "rejected-control",
                                                               "disconnect", "stale-hr", "cadence-collapse"])
    args = parser.parse_args()
    if not 1 <= args.steps <= 3600:
        parser.error("steps must be within 1..3600")
    return asyncio.run(run(args.steps, args.scenario))


if __name__ == "__main__":
    raise SystemExit(main())
