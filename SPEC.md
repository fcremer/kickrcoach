# KICKR Coach — Product / Technical Specification

## 1. Purpose

KICKR Coach is a local indoor-cycling application that controls a Wahoo KICKR CORE 2, records synchronized trainer and heart-rate data, executes reproducible structured workouts and uses longitudinal evidence to improve future workout prescription.

It is not intended to continuously force heart rate into a narrow zone. Mechanical power is the primary prescription; HR, cadence, RPE and readiness provide monitoring and interpretation.

## 2. Scope V1

V1 shall provide:

- BLE discovery and connection on macOS
- Wahoo KICKR CORE 2 telemetry over FTMS
- safe FTMS control acquisition/release
- ERG target-power control
- Apple Watch HR via HeartCast/iPhone using standard BLE Heart Rate Service
- generic compatibility with standard BLE HR straps
- YAML workout definitions
- deterministic workout engine
- safety controller and emergency stop
- mobile-first local web UI
- SQLite session storage
- CSV/session-summary export
- readiness/RPE capture
- post-workout analysis
- simulated trainer and HR source for development/tests

Explicitly out of V1: cloud accounts, Apple HealthKit integration in Python, Strava/TrainingPeaks integration, ANT+ control, virtual worlds/routes, AI/LLM in the real-time control loop, automatic FTP claims from insufficient data, FIT export, fully autonomous training progression.

## 3. Hardware assumptions

Primary trainer: Wahoo KICKR CORE 2.
Primary HR path: Apple Watch -> HeartCast watch/iPhone apps -> iPhone BLE broadcast -> macOS KICKR Coach.

HeartCast must be treated as replaceable. KICKR Coach consumes the standard BLE Heart Rate Service, not a private HeartCast API.

The physical hardware capabilities and macOS BLE behavior are unproven until Milestone M0 succeeds.

## 4. Functional requirements

### FR-001 Device discovery
Scan BLE devices and identify FTMS-capable trainers and Heart Rate Service sources. Allow explicit selection and persist a stable platform identifier where possible. macOS may expose CoreBluetooth UUID identifiers rather than MAC addresses; code must not require a MAC address.

### FR-002 FTMS capability probe
On KICKR connection, enumerate GATT services/characteristics/properties, read Fitness Machine Feature and Supported Power Range where exposed, subscribe to Indoor Bike Data and Fitness Machine Status where exposed, and log capability results.

Unsupported optional FTMS characteristics must cause graceful feature degradation, not crashes.

### FR-003 Trainer telemetry
Decode Indoor Bike Data flags correctly. At minimum expose instantaneous power and cadence when the trainer provides them. Keep source timestamps/receive timestamps. Never assume every notification contains every possible field.

### FR-004 FTMS control
Enable indications for the Fitness Machine Control Point, request control, serialize procedures and wait for protocol responses. Only after successful ownership may the application send target-power/start/stop commands.

No control-point command may be considered successful merely because the BLE write call returned.

### FR-005 Power bounds
Read supported power range where possible. Validate all target requests against trainer capability plus application safety configuration. Never hardcode trainer protocol limits as the only validation source.

### FR-006 Heart-rate input
Subscribe to standard Heart Rate Measurement notifications and parse 8-bit/16-bit BPM flags correctly. RR intervals may be retained when available but are not required for V1 decisions.

Maintain freshness:

- LIVE: recent notifications
- STALE: notifications delayed beyond configurable threshold
- LOST: no valid sample beyond configurable loss threshold/disconnected

Exact thresholds shall be tuned during M0; defaults may start around 5 s stale / 15 s lost.

### FR-007 Concurrent BLE
Maintain KICKR and HR-source connections concurrently for at least 30 minutes in the production architecture. M0 acceptance starts with a 10-minute proof and should be extended before V1 release.

### FR-008 Workout format
YAML workouts are schema-validated via Pydantic. Blocks support name, duration, control mode, target as absolute watts or percentage of configured indoor FTP/baseline, optional cadence guidance and optional notes.

Workout definitions are snapshotted into the session record so later edits do not alter historical interpretation.

### FR-009 Workout engine
Implement IDLE/PREPARING/READY/RUNNING/PAUSED/COMPLETED/ABORTED states. Use monotonic time. Pause must stop progression of workout elapsed time.

### FR-010 ERG transitions
Target changes pass through SafetyController. Large transitions may be ramped using configurable steps. Do not assume a smooth UI target means the trainer acknowledged each command.

### FR-011 Manual override
UI supports -5 W / +5 W default adjustments, pause/resume and STOP. Manual target changes are bounded and logged with reason/time. Define whether override lasts for current block or session; default current block.

### FR-012 Emergency stop
STOP immediately cancels scheduled/adaptive commands, marks the workout aborting/aborted, attempts FTMS stop/pause, stops further target writes and flushes session state. Local stop semantics must complete even if BLE communication fails.

### FR-013 Trainer disconnect
During RUNNING, loss of trainer connection pauses workout timing and invalidates control ownership. Reconnect asynchronously. After reconnect: resubscribe, reacquire control, enter safe recovery target/state and require explicit resume. Never restore a high target blindly.

### FR-014 HR disconnect
HR loss must not terminate a static workout. Mark HR unavailable, disable any HR-dependent adaptive feature and attempt reconnect. Never interpret missing HR as low HR.

### FR-015 Cadence safety
Avoid a universal rule that low cadence is always unsafe because low-cadence work can be intentional. Safety must consider workout cadence guidance and a separate anti-stall condition. For ordinary ERG blocks, detect sustained cadence collapse and reduce/pause target conservatively to prevent an ERG resistance spiral. Thresholds are configurable and logged.

### FR-016 Pre-session readiness
Capture optional compact readiness input: overall state and legs state, plus optional note. It informs analysis; it must not silently rewrite the workout.

### FR-017 Post-session RPE
Capture RPE and an optional question such as whether the rider could have continued ~20 minutes. Store with the session.

### FR-018 Recording
Persist synchronized samples at approximately 1 Hz and event-level records immediately. Store target, actual power, cadence, HR/freshness, connection states, block, manual/safety changes and timestamps.

### FR-019 Export
Provide CSV telemetry export and machine-readable JSON session summary. JSON should be suitable for later external analysis without exposing secrets/device identifiers unnecessarily.

### FR-020 UI
Local mobile-first dashboard must prominently show actual power, target power, HR/freshness, cadence, elapsed/remaining time, current/next block, connection state, -5/+5 W, pause/resume and STOP. Speed is optional and secondary.

### FR-021 Post-workout analysis
Calculate robust descriptive metrics. Compare HR response only over suitable steady-state windows. Store sufficient data for longitudinal comparison at similar power. Do not label a physiological threshold from one session.

### FR-022 Training progression
V1 may generate a recommendation for a future target adjustment based on repeated evidence, but persistent changes require explicit user confirmation. The real-time controller must remain deterministic.

## 5. Training-methodology requirements

### TM-001 Power first
Steady endurance workouts are prescribed primarily by KICKR power. HR is not continuously chased.

### TM-002 Indoor baseline
Treat indoor power scale independently until validated. Do not assume an outdoor/bike-display FTP transfers exactly to KICKR.

### TM-003 Baseline phase
Provide initial calibration/baseline workouts before relying heavily on percentage-based prescriptions. Example starting protocol may use progressive submaximal blocks (e.g. 90/105/115/125 W), but values must remain editable and are not a physiological diagnosis.

### TM-004 HR drift
HR drift is expected and can be informative. Do not automatically reduce power solely because HR rises gradually during a steady endurance block.

### TM-005 Context
RPE/readiness/cooling state are first-class session context. A fan/cooling flag is stored because indoor thermal conditions can materially affect HR response.

### TM-006 ERG limitations
Assessment/sprint/free-pacing features must be able to avoid ERG in a later mode. V1 shall not pretend ERG is optimal for every workout type.

## 6. Safety requirements

- No direct FTMS writes outside the trainer-control component.
- SafetyController validates every requested target.
- Manual STOP has highest priority.
- HR data never blocks STOP.
- Missing/stale sensor values are explicit states, never silently reused indefinitely.
- Control acknowledgement/timeout is handled explicitly.
- Reconnect never silently resumes previous high load.
- Simulator includes disconnect, timeout, rejected-control and cadence-collapse scenarios.
- Application is training software, not a medical device; no medical alerts/diagnoses.

## 7. Non-functional requirements

- Python >=3.12.
- asyncio-based concurrency.
- BLE implementation based on Bleak or a thin adapter around a proven library; do not couple domain logic to a third-party BLE library.
- FastAPI + WebSocket is preferred for local API/live UI.
- SQLite for V1 persistence.
- Pydantic validation for config/workout schemas.
- pytest unit/integration tests.
- Structured logging with separate domain events.
- Offline operation.
- No secrets in repository/logs.
- Clean shutdown releases resources and persists session state.

## 8. Suggested repository layout

```text
kickrcoach/
  app/
    main.py
    config.py
    trainer/
      interface.py
      scanner.py
      ftms.py
      kickr.py
      simulator.py
    sensors/
      heart_rate.py
      simulator.py
    workout/
      models.py
      parser.py
      engine.py
      safety.py
      adaptive.py
    recording/
      database.py
      recorder.py
      metrics.py
      export.py
    api/
      routes.py
      websocket.py
    models/
  frontend/
  workouts/
  tests/
    unit/
    integration/
  data/
  SPEC.md
  ARCHITECTURE.md
  AGENTS.md
  MILESTONES.md
  pyproject.toml
```

The exact layout may evolve if Codex documents a better reasoned design.

## 9. Initial sample workout

Use only after basic hardware control is proven:

```yaml
name: Baseline Progressive 45
version: 1
blocks:
  - {name: Warmup, duration_sec: 600, control_mode: erg, target_power_w: 90}
  - {name: Step 1, duration_sec: 600, control_mode: erg, target_power_w: 105}
  - {name: Step 2, duration_sec: 600, control_mode: erg, target_power_w: 115}
  - {name: Step 3, duration_sec: 600, control_mode: erg, target_power_w: 125}
  - {name: Cooldown, duration_sec: 300, control_mode: erg, target_power_w: 90}
```

This is an editable engineering baseline workout, not a validated individual training-zone determination.

## 10. Definition of V1 success

A user can start the local app, connect the CORE 2 and HeartCast, choose a validated workout, acquire trainer control, execute a stable ERG workout with clear manual/safety controls, see synchronized power/cadence/HR, survive sensor disconnects safely, finish/abort cleanly, enter RPE, and review/export a complete session. All critical control behavior is covered by simulator-backed automated tests and real-hardware acceptance tests.
