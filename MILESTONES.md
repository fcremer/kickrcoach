# Milestones

Development is gated. Do not continue past a milestone with unresolved critical acceptance failures.

## M0 — Hardware feasibility spike

Goal: remove the largest unknowns before application development.

Deliverables:

- minimal Python project / CLI
- BLE scanner
- KICKR FTMS probe
- generic BLE HRS/HeartCast probe
- concurrent telemetry output
- serialized FTMS control-point experiment
- `docs/HARDWARE_SPIKE.md`

Acceptance:

- KICKR CORE 2 discovered on target Mac
- HeartCast/HRS source discovered
- concurrent connections established
- power and cadence received from KICKR
- HR received from HeartCast
- timestamped telemetry runs continuously for >=10 minutes without an unexplained fatal disconnect
- FTMS capabilities/properties recorded
- Request Control result verified via FTMS response, not BLE write alone
- after explicit user confirmation, a low target-power change is commanded and physically/telemetrically verified, then returned to a safe target/stopped
- Ctrl+C performs clean shutdown

If concurrent KICKR + HeartCast is unstable, investigate macOS/CoreBluetooth behavior and HeartCast/iPhone constraints before proceeding. A BLE chest strap remains a fallback source, not an architectural rewrite.

## M1 — Protocol/domain foundation + simulators

Goal: production-quality adapters without UI.

Deliverables:

- TrainerAdapter
- FTMS KICKR implementation
- HeartRateSource generic HRS implementation
- SimulatedTrainer
- SimulatedHeartRateSource
- typed telemetry models
- connection/control state machines
- unit tests for parsers and states

Acceptance:

- simulated and real adapters expose the same domain interface
- no workout-domain module imports Bleak directly
- FTMS control commands are serialized
- stale/lost HR states tested
- disconnect/control rejection/timeout paths tested

## M2 — Workout engine + safety

Goal: deterministic structured ERG execution.

Deliverables:

- Pydantic workout schema
- YAML parser
- monotonic workout engine
- SafetyController
- manual +/- target override
- pause/resume/STOP
- safe reconnect behavior
- baseline sample workout

Acceptance:

- simulator executes complete multi-block workout
- block transitions occur correctly
- pause freezes workout progression
- STOP cancels pending commands and marks aborted
- no target bypasses SafetyController
- trainer disconnect pauses session and never auto-restores high target
- cadence-collapse scenario invokes configured safety response

## M3 — Recording + local API/UI

Goal: usable ride application.

Deliverables:

- SQLite persistence
- sample/event recorder
- FastAPI API
- WebSocket live telemetry
- mobile-first dashboard
- readiness input
- post-session RPE input

Acceptance:

- dashboard shows target/actual power, cadence, HR freshness, time, current/next block and connection states
- +/-5 W, pause/resume and STOP function end-to-end
- samples persist approximately once per second
- events persist immediately
- session survives UI reconnect without losing workout state

## M4 — Export + analysis

Goal: trustworthy session review.

Deliverables:

- CSV export
- JSON summary export
- descriptive metrics
- target-compliance metric
- steady-state HR response/drift analysis with data-quality checks
- session summary UI

Acceptance:

- exported sample count/timestamps reconcile with database
- analysis excludes missing/stale HR appropriately
- drift metric is not produced when steady-state/data-quality criteria are unmet
- RPE/readiness/cooling metadata appear in summary

## M5 — Baseline workflow

Goal: establish an indoor training baseline on KICKR rather than assuming another power source transfers exactly.

Deliverables:

- guided progressive submaximal baseline workout(s)
- comparison of stable power blocks with HR/RPE
- user-confirmed baseline/endurance target setting

Acceptance:

- no automatic physiological threshold claim from one session
- recommendations include evidence and confidence/data sufficiency
- persistent profile changes require confirmation

## M6 — Longitudinal progression

Goal: use repeated sessions to suggest future training changes.

Deliverables:

- comparable-session/block analysis
- trend view for power/HR/RPE
- conservative recommendation engine

Acceptance:

- recommendation uses multiple qualifying observations
- reasons/evidence are inspectable
- user must approve target/profile changes
- no recommendation executes a live trainer command directly

## M7 — Optional adaptive guardrails

Goal: limited live adaptation only where justified.

This is intentionally late.

Possible behavior:

- sustained abnormal HR plus user/context signals can recommend/reduce target conservatively
- missing HR disables HR-dependent behavior
- safety overrides all adaptation
- increases are slower/more conservative than decreases

Acceptance:

- no single HR sample changes power
- gradual normal HR drift alone does not trigger continuous target chasing
- every adaptation is logged with reason and inputs
- configurable hard limits prevent runaway adjustments

## Future / non-V1

- resistance/level mode and assessment workflows
- FIT export
- optional chest-strap advanced RR analysis
- Strava/TrainingPeaks integrations
- additional trainer brands
- richer periodization/planning
