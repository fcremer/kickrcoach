# AGENTS.md — Codex instructions

## Mission

Implement KICKR Coach according to `SPEC.md` and `ARCHITECTURE.md`. Treat those files as product/architecture constraints, not suggestions. If implementation evidence contradicts an assumption, document it and adjust the design rather than hiding the discrepancy.

## Critical development rule

Do not build the complete application in one pass.

Start with Milestone M0 in `MILESTONES.md`. The real Wahoo KICKR CORE 2 + HeartCast/macOS BLE path is the principal technical uncertainty. Prove it before investing in UI, database or training analytics.

## Engineering rules

1. Use standard Bluetooth FTMS as the primary KICKR protocol.
2. Do not add proprietary Wahoo commands without documenting why FTMS is insufficient.
3. Discover GATT capabilities at runtime; optional FTMS features may be absent.
4. HeartCast is not a private integration. Consume standard BLE Heart Rate Service so a chest strap remains interchangeable.
5. Serialize all Fitness Machine Control Point procedures and wait for indications/results.
6. A successful BLE write is not equivalent to a successful FTMS procedure.
7. Only one component may write trainer control commands.
8. Every target-power request passes through SafetyController.
9. Safety and explicit user commands override workout/adaptive logic.
10. Never interpret stale/missing HR as a valid low value.
11. Never auto-resume a previous high target after reconnect.
12. Use monotonic time for workout scheduling.
13. Keep BLE/library details behind adapters/interfaces.
14. Provide simulator implementations and test failure paths without physical hardware.
15. No LLM or opaque ML in the real-time control loop.
16. Do not implement continuous HR chasing. Power is the primary workout prescription.
17. Do not claim physiological thresholds/FTP from insufficient data.
18. Keep the app local/offline and do not add telemetry or cloud services.
19. Never commit secrets, personal device identifiers or captured sensitive logs.
20. Prefer simple, observable code over premature abstraction.

## M0 implementation brief

Create the smallest possible diagnostic implementation that can:

- scan BLE on macOS
- identify an FTMS trainer and BLE Heart Rate Service source
- connect to KICKR CORE 2 and HeartCast concurrently
- enumerate and log KICKR GATT services/characteristics/properties
- read/decode KICKR Fitness Machine Feature and Supported Power Range if available
- subscribe to Indoor Bike Data and print timestamped power/cadence
- subscribe to Heart Rate Measurement and print timestamped BPM
- subscribe to Fitness Machine Control Point indications/status as required
- request trainer control safely
- perform a deliberately low-power manual control test only after explicit CLI confirmation
- cleanly stop/release/disconnect on Ctrl+C
- log notification intervals and disconnects

Do not implement workouts or web UI in M0.

For any real power-control test, require an explicit CLI confirmation and start at a low configurable target. Do not silently command resistance during discovery.

## M0 output

At completion, create/update `docs/HARDWARE_SPIKE.md` with:

- OS/Python version
- KICKR advertised name/identifier (redact stable personal identifiers if committing publicly)
- discovered FTMS characteristics/properties
- supported power range
- actual Indoor Bike Data fields observed
- HeartCast advertised name and HRS behavior
- observed notification frequencies
- 10-minute concurrent-connection result
- FTMS request-control result
- low-power set-target test result
- disconnect/reconnect observations
- blockers/deviations from SPEC

If M0 fails, stop and report the failure with raw-enough diagnostic evidence. Do not fake/simulate success and continue to M1.

## Testing discipline

- Unit-test binary packet parsing with fixed byte fixtures.
- Test state machines deterministically.
- Use fake clocks for workout timing tests where practical.
- Simulate command timeouts, rejection, BLE loss, stale HR and cadence collapse.
- Do not require BLE hardware in normal CI.
- Hardware tests are explicitly marked and opt-in.

## Dependencies

Prefer a minimal dependency set. `bleak`, `pydantic`, `PyYAML`, `FastAPI`, `uvicorn` and `pytest` are acceptable starting choices. `pycycling` may be evaluated as a reference/helper, but do not make the architecture depend on it before M0 establishes compatibility and maintenance suitability.

Pin/lock dependencies using a modern `pyproject.toml` workflow. Document macOS Bluetooth permission requirements.

## Code quality

Use typing throughout core domain code. Use dataclasses/Pydantic models where they add validation or clarity. Avoid global mutable state. Log state transitions and trainer-control commands/results. Separate protocol parsing from domain policy.

## Definition of done per milestone

A milestone is done only when its acceptance criteria pass, automated tests are present for testable behavior, documentation is updated, and known limitations are recorded. Do not silently defer failed acceptance criteria to a later milestone.
