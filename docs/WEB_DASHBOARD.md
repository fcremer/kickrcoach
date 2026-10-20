# Local telemetry dashboard preview

## Scope and sequencing

On 2026-10-20 the user explicitly requested an initial web application to inspect
values, ahead of the original M2/M3 ordering. This is a bounded read-only slice
of M3 on top of the M1 foundation; neither M0 nor M3 is declared accepted.
The user reported the probe working; full ten-minute stability, physical control
and shutdown evidence still need to be recorded in HARDWARE_SPIKE.md.

Branch: `feat/local-telemetry-dashboard`, based on `m1-adapter-foundation`
(commit `09a83a6`). Review against M1 to isolate the dashboard changes. Merge M1
before this branch; if M1 is squash-merged, rebase this branch's dashboard commit
onto the resulting main rather than duplicating M0/M1 changes in the review.
Main and the M0/M1 branch pointers were not changed. Local config.toml is ignored
because it contains device identifiers; no captured hardware data belongs in Git.

## Design and boundaries

- FastAPI serves packaged static HTML/CSS/JS and `/ws/telemetry`; no Node build,
  frontend framework, external assets, API for trainer commands or persistence.
- One application lifespan owns scan, device selection, adapters and cleanup.
  A single consumer per adapter drains its bounded event queue; browser clients
  receive independent snapshots at 1 Hz. Slow clients do not block collection.
- The same M1 interfaces and standard FTMS/HRS transports support simulation and
  hardware. Simulated values are clearly marked; hardware failures never fall
  back to simulation. Simulation emits example measurements without acquiring
  control or running a workout.
- Trainer values expire at the configured stale/lost boundaries (5/15 s by
  default), or immediately on disconnect. Missing fields are null; zero is valid.
  HR freshness comes from the HRS adapter, including sensor-contact handling.
- The browser clears current values after 3.5 s without server messages. Reopening
  the browser does not own or reset BLE. Its WebSocket retries every 2 s; BLE
  reconnection requires a server restart and never restores a target.
- History is a bounded 180-point, approximately one-Hz in-memory series. Values
  are snapshots, not averages. Sensor gaps remain gaps. Independent scales are
  labelled; the chart does not compare unlike units on one axis. No analysis,
  session recording, workout duration or physiological threshold is inferred.
- Bind only to IPv4 loopback. Host and WebSocket Origin are checked. No CORS,
  device identifiers in browser payloads, access logs or cloud services. Device
  discovery/setup failures show a generic local troubleshooting message.
- Ctrl+C cancels collection and disconnects both adapters. Tests assert zero
  FTMS writes across connect, collection, browser reconnect and shutdown.

## Verification

Automated coverage includes deterministic freshness boundaries, missing fields,
zero values, device loss, bounded history, partial setup failure, multiple browser
clients/reconnect, static delivery, blocked foreign Host/Origin and the hardware
construction path using fake transports. The normal CI suite needs no BLE hardware.
Real HTTP and WebSocket smoke checks on loopback returned HTTP 200 and simulated
live values. JavaScript syntax and Git whitespace checks pass.

Browser visual QA could not be performed: no browser connector was available and
native Safari automation lacked Computer Use permission. Desktop/mobile rendering
and real-device dashboard operation remain explicit manual acceptance items.
The current stack emits an upstream Starlette/AnyIO TestClient deprecation warning;
tests pass. There is no hardware validation claim.

## Next acceptance steps

1. Run the dashboard with actual config.toml; observe power/cadence/HR together.
2. Check desktop and narrow mobile viewport layout; device access remains local
   to the Mac (phone/LAN access is intentionally outside this preview).
3. Stop HeartCast broadcast: confirm stale/lost states and hidden current BPM.
4. Stop trainer notifications/disconnect: confirm no frozen live power/cadence.
5. Reload/open a second tab; verify collection and elapsed observation continue.
6. Ctrl+C and verify device disconnection; complete the separate M0 evidence log.

Workouts, target/pause/STOP controls, SQLite, readiness/RPE and exports remain in
their original milestones. A control-enabled UI requires the M2 safety/workout
engine and separate end-to-end acceptance, rather than adding buttons to this
telemetry-only service.

Implementation reference: https://fastapi.tiangolo.com/advanced/websockets/
