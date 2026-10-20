"""Explicit opt-in hardware diagnostic; default path opens no BLE adapter."""
import argparse
import asyncio
from dataclasses import dataclass, fields
import logging
import math
from pathlib import Path
import sys
import tomllib
from . import ble
from .probe import Probe, emit


@dataclass
class Config:
    duration_sec: float = 600
    scan_sec: float = 10
    stale_sec: float = 5
    lost_sec: float = 15
    command_timeout_sec: float = 5
    test_sec: float = 10
    target_w: int = 50
    maximum_w: int = 100
    training_maximum_w: int = 300
    hr_zone_boundaries: tuple[int, ...] | list[int] = ()
    trainer_id: str = ""
    hr_id: str = ""
    hardware: bool = False
    control_test: bool = False
    scenario: str = "normal"

    def validate(self) -> None:
        from .zones import HeartRateZones
        if not isinstance(self.hr_zone_boundaries, (list, tuple)):
            raise ValueError("hr_zone_boundaries must be a list of BPM boundaries")
        HeartRateZones(tuple(self.hr_zone_boundaries))
        for name in ("duration_sec", "scan_sec", "stale_sec", "lost_sec",
                     "command_timeout_sec", "test_sec"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.stale_sec >= self.lost_sec:
            raise ValueError("stale_sec must be less than lost_sec")
        if self.command_timeout_sec > 10 or self.test_sec > 30:
            raise ValueError("M0 command timeout <=10 s; power test <=30 s")
        if (type(self.target_w) is not int or type(self.maximum_w) is not int
                or not 1 <= self.target_w <= self.maximum_w <= 100):
            raise ValueError("M0 requires 1 <= target_w <= maximum_w <= 100")
        if type(self.training_maximum_w) is not int or not 1 <= self.training_maximum_w <= 2000:
            raise ValueError("training_maximum_w must be an integer within 1..2000 W")
        if not isinstance(self.trainer_id, str) or not isinstance(self.hr_id, str):
            raise ValueError("device identifiers must be strings")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["scan", "probe"], nargs="?", default="probe")
    p.add_argument("--config", type=Path)
    p.add_argument("--hardware", action="store_true", help="explicitly enable real Bluetooth")
    p.add_argument("--control-test", action="store_true", help="manual low-power test; prompts on TTY")
    p.add_argument("--trainer-id")
    p.add_argument("--hr-id")
    p.add_argument("--duration-sec", type=float)
    p.add_argument("--target-w", type=int)
    p.add_argument("--scenario", choices=["normal", "timeout", "rejected-control", "disconnect",
                                         "stale-hr", "cadence-collapse"], default="normal")
    return p


def load(args: argparse.Namespace) -> Config:
    values = {}
    if args.config:
        with args.config.open("rb") as f:
            values = tomllib.load(f)
        # Hardware and control flags can NEVER be enabled by a config file.
        allowed = {f.name for f in fields(Config)} - {"hardware", "control_test", "scenario"}
        if values.keys() - allowed:
            raise ValueError(f"unsupported config keys: {sorted(values.keys() - allowed)}")
    for key in ("trainer_id", "hr_id", "duration_sec", "target_w"):
        if getattr(args, key) is not None:
            values[key] = getattr(args, key)
    config = Config(**values, hardware=args.hardware, control_test=args.control_test,
                    scenario=args.scenario)
    config.validate()
    if config.hardware and config.scenario != "normal":
        raise ValueError("fault scenarios are simulation only")
    if config.hardware and args.command == "probe":
        if not config.trainer_id or not config.hr_id or config.trainer_id == config.hr_id:
            raise ValueError("scan first; provide two distinct explicit device identifiers")
    if args.command == "scan" and args.control_test:
        raise ValueError("scan never performs control tests")
    return config


async def scan(config: Config) -> None:
    if not config.hardware:
        emit("scan", simulated=True, trainer_id="SIM-TRAINER", hr_id="SIM-HR")
        return
    found = await ble.discover(config.scan_sec)
    for device, advert in found.values():
        # Identifiers shown only for local selection; do not commit this output.
        emit("scan", name=advert.local_name or device.name, identifier=device.address,
             services=advert.service_uuids)
    if not found:
        raise RuntimeError("no FTMS/HRS advertisements; check Bluetooth permission and broadcasting")


def main() -> int:
    p = parser()
    args = p.parse_args()
    try:
        config = load(args)
        if config.control_test and config.hardware:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("real control test requires interactive TTY confirmation")
            phrase = f"CONTROL {config.target_w} W"
            print(f"Manual trainer test: {config.target_w} W for <= {config.test_sec:g} s. "
                  "Prepare to pedal; stop/disconnect will be attempted. "
                  "Close other trainer-control apps.\n"
                  f"Type exactly {phrase!r} to permit this run:", flush=True)
            if input().strip() != phrase:
                print("Cancelled; no Bluetooth connection or control command issued.")
                return 2
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        if args.command == "scan":
            asyncio.run(scan(config))
            return 0
        return 0 if asyncio.run(Probe(config).run()) else 1
    except KeyboardInterrupt:
        print("Stopped by user; see stop/disconnect results above.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print backend messages which may contain stable personal identifiers.
        print(f"Probe failed ({type(exc).__name__}). Check configuration, Bluetooth permission, "
              "service capabilities and diagnostic events. M0 is not accepted.", file=sys.stderr)
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
        return 1
