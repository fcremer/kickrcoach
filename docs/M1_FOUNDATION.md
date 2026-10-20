# M1 — Protocol/domain foundation and simulators

## Status and scope

M1 software foundation implemented on top of M0 PR #2, following the user's
explicit request to implement M1 on 2026-10-20. This request advances software
implementation ahead of the original milestone sequencing; it does not replace
any real-hardware acceptance criterion. `docs/HARDWARE_SPIKE.md` remains the M0
hardware record and Issue #1 remains open. No M2 workout/safety engine, web UI,
database, recording or adaptive training was implemented.

M1 is simulator/transport-tested. Actual KICKR CORE 2 + HeartCast/macOS behavior
is unverified until the M0 procedure is run on the target Mac. Do not call this
hardware-validated or production-ready based on these tests.

## Components and contracts

| Component | Responsibility |
| --- | --- |
| `domain.py` | Runtime-checkable `TrainerAdapter` / `HeartRateSource`, immutable typed models, capabilities and state enums |
| `transport.py` | Small injectable GATT contract without Bleak or platform-specific objects |
| `ble.py` | Bleak wrapper, discovery/selection and transport factory; opaque macOS UUIDs supported |
| `trainer.py` | FTMS capability probe, subscriptions, state transitions and command lifecycle |
| `control.py` | Only FTMS writer; serialized procedures and matched indication results |
| `heart_rate.py` | Generic standard HRS source, contact handling and notification-independent freshness events |
| `simulation.py` | `SimulatedTrainer`, `SimulatedHeartRateSource`, virtual clock and fault-injectable in-memory transport |
| `demo.py` | Executable adapter-only simulation (no workout engine or Bluetooth access) |

The original M0 diagnostic path is retained for hardware evidence gathering.
The M1 simulators use the real FTMS/HRS adapter implementations, parsers and
control writer against an in-memory transport. Shared contract tests instantiate
both the base adapters with injectable transports and the named simulators.

`TrainerAdapter`: connect/disconnect, capabilities, request_control,
set_target_power, pause, resume, stop; connection/control state, target_power_w
and an asynchronous event queue. `HeartRateSource`: connect/disconnect, latest,
refresh, connection state and the same event-queue contract.

Event records are frozen dataclasses. Trainer packets retain missing fields as
`None` rather than carrying an old measurement forward. UTC receive time and
monotonic receive time are retained; unsupported device-origin timestamps are
`None`. Heart-rate freshness uses the monotonic clock, independent of wall-clock
changes. A sample has separate observation time and last valid receive time.
STALE/LOST measurements expose `bpm=None` and no current RR intervals.

Event queues are bounded single-consumer diagnostics (default capacity 256),
dropping the oldest queued event when full and incrementing `dropped_events`.
Historical queued samples retain their original timestamps; they must not be
interpreted as current sensor state. Adapter properties / `hr.latest` are the
current authoritative state. This is not a lossless recorder or safety command
channel; M3 must provide its own recording requirements.

## Connection/control behavior

| Operation/event | Preconditions | Result |
| --- | --- | --- |
| Connect | DISCONNECTED and previous transport cleaned up | CONNECTING → CONNECTED/READONLY, fresh subscriptions/capabilities; zero control writes |
| Request Control | CONNECTED/READONLY, CP write + indications available | ACQUIRING → READY only after FTMS success |
| Set target | READY/RUNNING/PAUSED, confirmed power-target feature + range | Bounded acknowledged target; every request passes SafetyController |
| Resume/start | READY or PAUSED, explicitly acknowledged target in this session | RUNNING after FTMS success |
| Pause | RUNNING | PAUSED after FTMS `08 02` success |
| Stop | Any state | Local target invalidated first; pending/queued commands cancelled or rejected; best-effort acknowledged `08 01` |
| Confirmed normal Stop | Connected with unambiguous control | READONLY; explicit reacquisition/target/start required |
| Timeout/rejection/cancelled procedure | Any outstanding procedure | DEGRADED/UNCERTAIN; no further target/start permitted |
| Reset/Control Permission Lost | Current BLE session | Control invalidated; reconnect required |
| Unexpected trainer disconnect | Current session | DEGRADED then DISCONNECTED; target cleared; no automatic reconnect or resume |
| Explicit disconnect | Any state | Stop attempt, bounded disconnect, clear capabilities and session callbacks |
| Disconnect failure | Cleanup attempted | DEGRADED; transport retained so cleanup can be retried; reconnect refused |

All operations in a session are serialized. STOP increments a generation counter
before waiting, cancels active control work, and causes previously queued targets
to fail rather than executing after STOP. The current target is only published
after a successful protocol response. Old-session notifications and responses
cannot restore ownership or overwrite telemetry after reconnect.

FTMS responses contain an opcode but no transaction identifier. After a timed-out
or cancelled procedure, its opcode is quarantined until a new connection. In
particular, a late Pause response (`08`) must not falsely acknowledge a later
Stop (`08`). Such a Stop is reported as unconfirmed, requiring physical action
if needed, rather than claiming that resistance was released. Disconnect still
runs. There is no separate FTMS Release Control opcode or restoration of an
unknown prior load.

HR connect/reconnect always starts LOST without a cached current BPM. Malformed
notifications do not reset freshness; reported loss of sensor contact invalidates
the value. The default thresholds remain 5 s STALE / 15 s LOST and are configurable.
A small background watcher publishes freshness transitions even when no packets
arrive. Tests use `ManualClock.advance()` plus `refresh()` without waiting for
real seconds. HR disconnect never invokes a trainer command. Reconnect is
explicit in M1; workout recovery and retry/backoff policy remain future work.

Optional FTMS reads and subscriptions fail with diagnostic events. Missing or
malformed feature/range data disables target power; failed CP indications/write
support disables control. Missing status support is reported. Read-only telemetry
continues where possible. Missing required Indoor Bike Data/HRS notifications
fails setup and cleans up the connection. Operations have bounded timeouts.

## Simulation and validation

```sh
uv sync --locked --extra dev
uv run --locked pytest -q
uv run --locked kickrcoach-adapters --steps 20
uv run --locked kickrcoach-adapters --scenario stale-hr --steps 20
uv run --locked kickrcoach-adapters --scenario cadence-collapse
uv run --locked kickrcoach-adapters --scenario timeout
```

The demo is a fixed low-power diagnostic sequence with virtual time, not a
training prescription or workout engine. Cadence collapse is observable telemetry
in M1; an automatic anti-stall workout response belongs to M2. The M0 manual
experiment retains its own conservative stop guards.

| M1 acceptance criterion | Software evidence |
| --- | --- |
| Same interfaces for real/simulated adapters | Shared contract tests and runtime protocol checks |
| Domain free of Bleak dependency | Only `ble.py` imports Bleak; no workout-domain module exists yet |
| Serialized FTMS procedures | Concurrent target tests; shared single writer; matching result tests |
| HR STALE/LOST tested | Manual-clock boundaries, wall-clock jump, contact/invalid data, packet-independent freshness events |
| Disconnect/control rejection/timeout tested | Failure fixtures, explicit safe reconnect, old-session callback rejection, stop/cancellation and retryable cleanup |

Real BLE behavior, 10/30-minute stability and physical target/Stop response remain
outside the results obtained in this environment. The 100 W engineering ceiling
is intentionally preserved until the hardware evidence and M2 policy justify
changing it. No new dependency, cloud call or proprietary Wahoo command was added.
