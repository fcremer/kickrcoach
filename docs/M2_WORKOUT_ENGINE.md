# M2 — deterministic workout engine and safety

## Status and scope

Implemented at the user's explicit request after the local telemetry preview.
M2's automated simulator acceptance criteria are covered; the outstanding M0
hardware criteria remain open in issue #1. No real workout/control session was
performed and this change enables no hardware CLI or web control. The existing
read-only dashboard and M0 explicitly confirmed low-power experiment remain the
hardware paths. No SQLite, M3 recording, readiness/RPE, analytics or adaptation.

## Use

```sh
uv sync --locked --extra dev
uv run --locked kickrcoach-workout workouts/baseline.yaml
uv run --locked kickrcoach-workout workouts/baseline.yaml --scenario stale-hr
uv run --locked kickrcoach-workout workouts/baseline.yaml --scenario cadence-collapse
uv run --locked kickrcoach-workout workouts/baseline.yaml --scenario disconnect
uv run --locked --extra dev pytest -q
```

This CLI is always simulated and advances virtual time one second per step.
Normal and stale-HR runs complete with exit 0. Cadence collapse and disconnect
pause and exit 1 without supplying resume consent. Timeout/rejected-control also
exit 1; Ctrl+C performs cleanup and exits 130. Each JSON line is marked simulated.
The example's 40/70/40 W blocks total four minutes; they are a low-power software
exercise, not an FTP assessment or personal training prescription.

## Definition

`workout.py` validates immutable Pydantic models, loaded with a SafeLoader that
also rejects duplicate YAML keys. Files are limited to 256 KiB. Unknown fields,
invalid durations, ambiguous/missing targets, unsupported modes, non-finite
percentages and invalid cadence guidance fail validation. Example:

```yaml
version: 1
name: Easy example
blocks:
  - name: Steady
    duration_sec: 120
    mode: erg
    target:
      percent_baseline: 50
    cadence:
      minimum_rpm: 70
      maximum_rpm: 95
    notes: Optional guidance
```

A target specifies exactly one of `watts` or `percent_baseline`. Percentage
resolution requires an explicitly provided indoor baseline (`--baseline-w` in
the simulator); the engine never estimates one. Values round to the nearest
integer watt with half-watts rounded up. The frozen definition and resolved
watts are held for the session; M3 must persist that snapshot with its recording.
Only ERG is implemented; the explicit `mode` field retains the extension point.

## Runtime ownership and timing

`WorkoutEngine` depends only on the domain adapter interfaces, a clock and
policies. Its public API is prepare/start/tick/pause/resume/adjust/stop/close.
The runtime owner calls tick regularly (recommended 4 Hz) and must call close in
finally. The current executable is a virtual-clock runtime; no production
hardware workout runner is exposed yet. Each engine exclusively consumes its
adapters' event queues. Do not run it and the telemetry-only web collector on
the same adapter instances; M3 must broadcast from a shared session owner.

PREPARING connects and validates every resolved target against capabilities and
SafetyController before any Request Control. READY is read-only. Start requires
fresh power and safe positive cadence, then acknowledged Request Control → target
→ Start. RUNNING time begins only after Start succeeds. Boundaries use accumulated
monotonic time, never wall time. If a tick crosses several block boundaries, only
the currently due block is applied, without replaying expired targets in a burst.

Pause freezes progression before awaiting the acknowledged FTMS Pause. Resume is
explicit and requires fresh trainer telemetry/cadence. Manual -5/+5 W adjustment
is valid during RUNNING, bounded, acknowledged and logged. It overrides only the
current block; the next block clears it. Invalid manual input is rejected while
a healthy workout continues. No target is published as commanded before adapter
acknowledgement. COMPLETED requires the end-of-workout Stop procedure to succeed;
local errors cannot be presented as confirmed physical release.

## Safety and priority

- The shared `SafetyController` remains the final validator in both engine and
  M1 adapter/control writer. The configured maximum remains <=100 W pending real
  hardware acceptance; higher workouts fail preflight rather than being silently
  clamped. Range and increment come from runtime trainer capabilities.
- `WorkoutGuard` handles trainer freshness and sustained cadence collapse. Default
  telemetry timeout is 5 s, collapse duration 3 s, ordinary floor 35 rpm and
  anti-stall floor 5 rpm. Missing power/cadence is unavailable, not a reused value.
- With explicit low-cadence guidance, the collapse floor is lowered to half the
  guidance minimum, bounded by the anti-stall floor. Sustained zero cadence still
  pauses. These configurable engineering defaults require hardware tuning; they
  are not physiological thresholds. Safety pause uses acknowledged FTMS Pause,
  freezes time and requires explicit resume after cadence recovers.
- HR is observational: stale/lost HR stays unavailable in hr.latest and never
  raises or lowers power. HR reconnect runs independently of trainer ticks, with
  a default 5 s retry interval. No HR chasing/adaptive loop exists.
- Trainer loss/control uncertainty freezes time in PAUSED. Subsequent ticks retry
  disconnect/connect/subscription/control acquisition with 5 s backoff. Recovery
  confirms at most min(50 W, block prescription), rounded down to the discovered
  increment; if no valid low target exists it refuses recovery. No Start is sent.
  New-session live telemetry and explicit resume are required. The recovery
  target overrides the remainder of the current block; an explicitly resumed
  session returns to prescription at the next block boundary.
- STOP invalidates queued work and marks ABORTED before waiting for BLE. It cancels
  active work and attempts Stop. A failed Stop is logged as unconfirmed, with
  physical action potentially required. It never restores a target. Cancellation
  completion waits for the engine operation rather than its caller's whole task,
  allowing the caller to close safely in finally without deadlocking.
- Only the existing TrainerControl writes FTMS commands. Rejected/timed-out
  procedures remain uncertain; M1's late-response quarantine is retained.

Events include monotonic time, workout state/reason, elapsed time, block and
acknowledged target. The 256-entry diagnostic queue drops oldest entries with a
counter; it is explicitly not M3's lossless event recorder. There is no ramping
in this version (optional in FR-010); each block uses one validated target.

## Acceptance evidence

| M2 acceptance | Automated evidence |
| --- | --- |
| Complete multiblock simulation | 240 s example CLI and exact-boundary engine test |
| Correct block transitions | Boundary, wall-clock jump and override reset tests |
| Pause freezes progression | Virtual 60 s pause with unchanged elapsed time |
| STOP cancels pending commands/aborts | Pending target + queued override, failed Stop, cancellation/finally-close tests |
| Every target passes SafetyController | Preflight future target rejection; shared validator spy for start/manual/block/recovery paths |
| Disconnect pauses; no high-target restore | 90 W disconnect recovers at 50 W; no Start until explicit resume and fresh telemetry |
| Configurable cadence-collapse response | Sustained zero cadence Pause, guided low cadence, invalid config and blocked unsafe resume tests |

Additional tests cover YAML validation/snapshot immutability, baseline percentages,
missing/stale trainer fields, HR loss/nonblocking reconnect, control rejection,
timeout, failed recovery/backoff, increment alignment, terminal state guards and
bounded diagnostics. CI runs the simulator without BLE hardware.

Remaining real-hardware acceptance: complete M0's verified FTMS control/Stop and
stability evidence before exposing a hardware workout runner or web control.
No simulator result substitutes for that evidence.

References: https://docs.pydantic.dev/latest/concepts/models/ and
https://pyyaml.org/wiki/PyYAMLDocumentation
