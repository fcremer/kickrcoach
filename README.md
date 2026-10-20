# KICKR Coach — M0 hardware feasibility probe

This first executable version implements Issue #1 only. It scans/probes standard
Bluetooth FTMS trainers and HRS heart-rate sources, including the intended
KICKR CORE 2 + HeartCast path. Hardware compatibility is **not yet proven**.
M1+ (workouts, web UI, database, analytics, adaptation) remains gated on real M0 acceptance.

## Install and test

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for the locked workflow:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q
uv run --locked kickrcoach probe --duration-sec 3
uv run --locked kickrcoach probe --duration-sec 4 --control-test
```

The default is simulation. It does not import Bleak's backend, scan Bluetooth or
command any physical device. `python -m kickrcoach` is also supported after install.
An alternative is `python -m pip install '.[dev]'` (direct dependencies pinned;
use uv for the complete platform-aware lock).

Simulation fault probes:

```sh
uv run kickrcoach probe --duration-sec 5 --scenario disconnect
uv run kickrcoach probe --duration-sec 5 --control-test --scenario timeout
uv run kickrcoach probe --duration-sec 5 --control-test --scenario rejected-control
uv run kickrcoach probe --duration-sec 6 --control-test --scenario cadence-collapse
```

`stale-hr` stops sending HR after two samples. Run longer than 17 seconds to see
LIVE → STALE → LOST. For a manual test longer than the default 10 seconds,
use `test_sec` in a local config (maximum 30 seconds). HR is never used to chase
a training zone. Loss during this short diagnostic control experiment aborts
that experiment; this is not the later workout policy in SPEC FR-014.
Fault scenarios are refused with `--hardware`.

## On the target Mac

Enable Bluetooth and allow your terminal/IDE under System Settings → Privacy &
Security → Bluetooth. Bleak uses CoreBluetooth identifiers (UUIDs), not MAC
addresses on macOS. Start HeartCast on Apple Watch **and iPhone**, check that the
watch HR reaches the phone, and enable its BLE broadcast. No Python HealthKit
access or private HeartCast API is assumed. A standard BLE chest strap can replace it.
Keep the phone nearby and verify broadcast continuity/screen-lock behavior during
M0. Close MyWhoosh, Wahoo, Zwift or other apps that could own trainer control.
Use the bike with its motor off and an appropriate, checked trainer setup.

```sh
uv run kickrcoach scan --hardware
cp config.example.toml config.local.toml
# Enter trainer_id and hr_id from this Mac's scan in config.local.toml.
uv run kickrcoach probe --hardware --config config.local.toml --duration-sec 600
```

Scan lists local names, platform identifiers and service UUIDs. Selection requires
explicit identifiers and the advertised FTMS/HRS service. If a device omits its
service advertisement, record that blocker rather than guessing by its name.
Normal probing only reads/subscribes: **no Request Control, target, start or stop
writes occur**. Optional feature/range/status/control capabilities degrade with
diagnostic events; power/cadence/HR availability is still required for acceptance.

The probe logs GATT properties, feature bit masks and supported power range,
decoded power/cadence/HR, UTC receive timestamps, monotonic receive times,
notification intervals, freshness and disconnects. Missing packet fields are not
silently copied forward. No source timestamp is invented when BLE has none.

## Deliberate low-power control experiment

```sh
uv run kickrcoach probe --hardware --config config.local.toml --control-test --target-w 50
```

An interactive TTY is required. Type the exact displayed `CONTROL 50 W` phrase
before Bluetooth is opened. There is no unattended `--yes` switch; configuration
cannot enable hardware or control. Prepare to pedal before running the command.
The probe waits up to `lost_sec` for live HR, power and positive cadence before
requesting control. Target power must match the discovered range/increment and
the configured bound, with an absolute M0 ceiling of 100 W. Unknown range or
unconfirmed power-target support blocks the test.

The sole control writer subscribes to indications, serializes procedures and
checks `0x80 / requested opcode / result` responses. A BLE write alone is never
success. The sequence is Request Control → Set Target Power → Start/Resume →
bounded observation (default 10 s) → Stop. Observe the load change yourself and
inspect actual KICKR power/cadence; protocol acknowledgement alone is not physical
verification. Never infer FTP or an indoor training zone from this experiment.

Ctrl+C cancels the probe and attempts an acknowledged Stop if control was
attempted, then disconnects both devices. Reset/control-permission loss,
command timeout/rejection, disconnected sensors, and stale/zero-cadence telemetry
during the manual test abort it. There is no auto-reconnect or high-load restore.
FTMS has no separate Release Control command: stop and disconnect are used.
If Stop fails, the output explicitly requires a physical stop/unload; a software
shutdown cannot guarantee the trainer has released resistance. Do not restore an
unknown previous target. Discovery/read-only cancellation never sends Stop.

## Evidence and exit codes

- `0`: short smoke probe completed with fresh power/cadence/HR, or the telemetry
  checks for a >=600 s run passed. This does **not** declare full M0 acceptance.
- `1`: configuration, transport, control or telemetry check failed.
- `2`: confirmation cancelled; `130`: Ctrl+C.

Output is JSON lines on stdout; control diagnostics are on stderr. For a read-only
run you can capture evidence locally:

```sh
uv run kickrcoach probe --hardware --config config.local.toml > hardware.local.jsonl 2> hardware.local.log
```

Do not pipe/redirect an interactive control run's stdout: the confirmation must
remain visible. Run it in the terminal and retain only reviewed, redacted results.
Device identifiers/names appear in scan output; HR logs are personal data. Local
config, JSONL and log files are ignored. Do not commit raw captures.
Summaries include sample counts, maximum notification gaps, invalid-packet counts
and whether 10-minute concurrent telemetry was observed. Review the raw evidence
for field-specific dropouts and physically verify the low-power test.
Update [docs/HARDWARE_SPIKE.md](docs/HARDWARE_SPIKE.md) with actual Mac findings;
Issue #1 stays open until the real acceptance checklist is satisfied.

## Components and constraints

- `protocol.py`: pure, fixture-tested FTMS/HRS decoding.
- `ble.py`: explicit discovery/selection and Bleak boundary.
- `control.py`, `safety.py`: one serialized control path and M0 target validation.
- `sensors.py`: receive-time HR freshness (default 5 s stale / 15 s lost).
- `probe.py`: concurrent M0 orchestration, diagnostic output and cleanup.
- `simulator.py`: in-memory transport with fault scenarios.
- `cli.py`: validated TOML/CLI configuration and explicit hardware consent.

The simulator is a diagnostic test double, not a claim of CORE 2/HeartCast
compatibility and not the full M1 adapter/state-machine implementation.
See [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[AGENTS.md](AGENTS.md) and [MILESTONES.md](MILESTONES.md).

Protocol references: [Bluetooth SIG FTMS](https://www.bluetooth.com/specifications/specs/fitness-machine-service-1-0-1/),
[Bluetooth SIG HRS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html),
[Bleak macOS backend](https://bleak.readthedocs.io/en/latest/backends/macos.html).
