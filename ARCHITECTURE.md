# Architecture

## Design goal

Build a deterministic, observable indoor-training controller. Reproducible power prescription is the control variable. HR, cadence and RPE provide context. Longitudinal analysis should improve future workout prescriptions; live adaptation is deliberately conservative.

## High-level architecture

```text
Workout Definition (YAML) ──> Workout Engine ──> Safety Controller ──> TrainerAdapter ──> KICKR CORE 2
                                    │                                      ▲
                                    │                                      │ FTMS telemetry
                                    ▼                                      │
                              Session Recorder <────────────────────────────┘
                                    ▲
                                    │
Apple Watch -> HeartCast/iPhone -> BLE Heart Rate Service -> HeartRateSource
                                    │
                                    ▼
                              Analysis Engine
                                    ▲
                                    │
                         RPE / readiness input
```

## Architectural decisions

### AD-001 — FTMS first
Use standard Bluetooth Fitness Machine Service for KICKR communication. Proprietary Wahoo commands are prohibited unless an identified requirement cannot be met by FTMS and the deviation is documented first.

Relevant services/characteristics must be discovered at runtime. Do not assume the trainer exposes every optional FTMS feature.

Expected FTMS UUIDs include service 0x1826, Fitness Machine Feature 0x2ACC, Indoor Bike Data 0x2AD2, Supported Power Range 0x2AD8, Fitness Machine Control Point 0x2AD9 and Fitness Machine Status 0x2ADA. Verify the actual GATT table on the physical CORE 2 during M0.

### AD-002 — HeartCast is a generic HR source
Do not create HeartCast-specific workout logic. Implement the standard BLE Heart Rate Service (0x180D) / Heart Rate Measurement (0x2A37). HeartCast is the V1 source, but a BLE chest strap must be replaceable without changing the workout engine.

HR samples carry a receive timestamp and state: LIVE, STALE, LOST. Never keep treating the last BPM value as current after notifications stop.

### AD-003 — One serialized trainer-control path
Only TrainerControl may write to the FTMS Control Point. Commands are serialized. There must never be multiple outstanding control-point procedures.

### AD-004 — Safety is final authority
All requested power changes pass through SafetyController. Priority:

1. Emergency/manual stop
2. Safety state
3. Trainer connection/control state
4. Explicit user override
5. Workout prescription
6. Optional adaptive recommendation

Adaptive code can recommend but never directly command the trainer.

### AD-005 — Power is the training prescription
Do not implement a controller that continuously chases a narrow HR range. HR is initially observational and a guardrail. Cardiac drift is primarily a post-workout analytic. Training progression is applied between sessions after review/confirmation.

### AD-006 — ERG is not universal
Workout blocks carry a control mode. V1 prioritizes ERG for endurance/recovery/steady intervals. Architecture must allow a future resistance/level mode for assessments, sprints and free pacing. Do not perform maximal assessment logic under forced ERG by default.

### AD-007 — KICKR is indoor power source of truth
All indoor targets, compliance calculations and indoor FTP/baseline values use KICKR-reported power. Do not merge another bike power source into the same power scale without an explicit calibration/normalization design.

### AD-008 — Simulator before hardware-dependent application logic
Define `TrainerAdapter` and `HeartRateSource` protocols/interfaces and provide simulated implementations. CI must run without BLE hardware.

### AD-009 — Monotonic workout timing
Use monotonic time for workout progression. Wall-clock timestamps are used for persisted records only.

### AD-010 — Local first
No cloud dependency, analytics SDK or account system. FastAPI/WebSocket serves a local mobile-first UI. LAN exposure must be configurable; default to local-only during development.

## Core interfaces

```python
class TrainerAdapter(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def capabilities(self) -> TrainerCapabilities: ...
    async def request_control(self) -> None: ...
    async def set_target_power(self, watts: int) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def stop(self) -> None: ...

class HeartRateSource(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
```

Telemetry should be event/callback/queue based rather than synchronous polling APIs.

## State machines

Trainer connection:

```text
DISCONNECTED -> DISCOVERING -> CONNECTING -> CONNECTED_READONLY
-> ACQUIRING_CONTROL -> READY -> RUNNING/PAUSED

Any state -> DEGRADED -> DISCONNECTED/READY
```

Workout:

```text
IDLE -> PREPARING -> READY -> WAITING -> RUNNING <-> PAUSED -> COMPLETED
                                  \-> ABORTED
```

On trainer disconnect while running: pause workout timing, invalidate control ownership, reconnect, reacquire control, enter a safe recovery state and require explicit resume. Never blindly restore a high previous target.

## Data model

Persist at approximately 1 Hz, while retaining original event timestamps:

- session_id
- wall timestamp
- elapsed monotonic-derived seconds
- workout block
- planned target power
- commanded target power
- actual KICKR power
- cadence
- HR BPM and HR freshness
- trainer/HR connection states
- manual override
- safety intervention
- event/reason

Speed may be recorded if exposed but is not a primary ERG training metric and should not be prominent in the UI.

Session metadata includes workout snapshot, configured indoor FTP/baseline, duration, completion status, RPE, readiness, fan/cooling flag and optional notes.

## Analysis boundary

V1 post-workout analytics:

- average/max power
- average/max HR
- average cadence
- target compliance
- HR response by stable power block
- HR drift/decoupling only where data quality and steady-state duration make it meaningful
- RPE and completion

Do not infer physiological thresholds from a single ride. Recommendations must state confidence/data sufficiency and require user confirmation before changing persistent training parameters.

### AD-011 — Arm first start before pedaling; separate diagnostic/training limits

The user needs Start → mount → ride. A first GUI start may therefore enter
WAITING without any trainer-control writes or elapsed workout time. Fresh safe
pedaling triggers that already-requested low-power start. STOP cancels WAITING.
An immediate start remains possible when telemetry is already live. This is not
a recovery shortcut: an interrupted running workout still requires explicit
resume after low-target recovery.

The GUI's training_maximum_w is independent of M0's maximum_w. Both still pass
through SafetyController and discovered capabilities; measured power is not a
target request. Raising an operational training ceiling does not infer FTP or
change the M0 diagnostic ceiling. See docs/WEB_DASHBOARD.md for defaults and tests.


### AD-012 — Durable sampled sessions and user-directed export

A workout owner records engine events and approximately 1 Hz snapshots to local
SQLite independently of UI connections. Worker-thread transactions are joined
on cancellation; trainer STOP precedes persistence waits. A single archive
lease guards restart recovery. A prior active session becomes interrupted,
never resumed. Anchored UTC plus monotonic offsets preserve point order across
wall-clock changes; original sensor receipt times and quality remain explicit.

Review metrics exclude paused/stale/missing values and expose time coverage.
TCX is the portable ride export; versioned JSON preserves the richer local
context for user-directed AI review. No automatic cloud transmission or
real-time AI control is introduced. See docs/SESSION_RECORDING.md.
