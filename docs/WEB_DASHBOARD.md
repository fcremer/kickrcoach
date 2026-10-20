# Local training dashboard

Current capabilities: HR zones, independent device discovery/reconnect, simulated
workouts and explicitly CLI-enabled hardware ERG controls. Hardware remains
read-only without --enable-control. The sections below record the incremental
implementation; the final hardware-control section supersedes earlier
simulator-only restrictions. Full M3 recording acceptance is still pending.

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
  frontend framework, external assets or persistence. The later simulator-only
  command endpoint is described below; hardware remains read-only.
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
  reconnection is now independently retried as described below, with no control
  acquisition or target writes in hardware mode.
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

Real workout control, SQLite, readiness/RPE and exports remain gated on their
acceptance criteria. The subsequent simulator-only UI uses the M2 engine as
described below; it does not enable a real trainer-control path.

Implementation reference: https://fastapi.tiangolo.com/advanced/websockets/

## Pulse zones and simulator workout integration — 2026-10-20

The user requested a prominent HR zone view and supplied personal boundaries.
Those values are stored only in ignored config.toml. Tracked examples leave
`hr_zone_boundaries = []`; no default zone system or physiological thresholds are
inferred. The four configured values are the inclusive starts of Z2..Z5. Z1
contains lower positive BPM. Config validation requires four strictly increasing
integers within the standard HRS 16-bit range, or an empty list. Changing the
TOML file requires a server restart in either hardware or simulation mode.

The UI shows the current zone and its BPM range above a five-segment scale;
BPM is secondary. On narrow displays the zone card comes first. Zone labels and
numbers supplement color. Stale/lost/null HR or lost server messages clear the
zone highlight. Configured ranges remain inspectable. Classification is purely
presentation and never changes trainer targets. Config is broadcast only over
the existing local, same-origin WebSocket, with no device IDs added.

The default simulator now owns a single M2 WorkoutEngine and two simulated
adapters in WebWorkout. It runs 4 Hz engine ticks against real monotonic time,
while browser broadcasts/history stay approximately 1 Hz. Two embedded demo
workouts require no checkout-relative data file in installed wheels. Browsers
share one engine; reload never resets it. The UI shows acknowledged and planned
target, current/next block, elapsed/remaining workout time, selection and
start/pause/resume/-5/+5/STOP controls. An override is explicitly marked as lasting
to the next block. Selecting/reloading prepares a session but never starts it.

POST /api/workout accepts a strict action schema, rejects foreign/missing Origin,
and returns 403 for hardware or telemetry-only collectors. No option switches a
hardware session into controlled mode. This is a simulated M3 UI slice; there is
still no persistence or real workout control. Hardware display continues its
original read-only collector and zero-write guarantees.

STOP bypasses the simulator session lock and invalidates queued commands. Engine
operations run in isolated tasks so preempting one cannot kill the long-lived
telemetry collector. An interrupted FTMS procedure can leave the simulator's
adapter uncertain: its cached measurements are hidden, and loading a new workout
creates a clean read-only session without restoring the previous target.

Verification includes boundary/stale-HR classification, DOM state tests for zone
priority and workout gating, schema/config validation, real API/websocket command
sequences, shared browser state, read-only hardware rejection and STOP preemption
with telemetry continuation. Node's built-in test runner is used in CI; there is
no frontend dependency installation. A real-browser visual/mobile inspection
remains unverified because the available session lacks browser automation access.

## Independent hardware discovery and reconnect — 2026-10-20

The previous web startup required both selected devices to be present in one
scan, then waited on both connection attempts. The user identified this as an
unrealistic startup dependency. HardwareTelemetry now owns one scanning task,
one connection supervisor and event consumer per role, and one snapshot/history
loop. Discovery continues while either selected device is missing; each role
can connect as soon as its explicit identifier and standard service are found.
A connection/setup timeout for one sensor does not delay the other's telemetry.

The app starts with both roles waiting and serves the dashboard immediately.
Discovery retries after a three-second delay between scan windows; a scan window
uses the existing scan_sec setting (default 10 s). A healthy pair causes discovery
to idle. Device loss triggers cleanup/reconnection only for the affected role;
a setup failure retries after discovery without replacing a healthy other
transport. HR LOST without a transport disconnect is retried once the configured
loss threshold has elapsed. Configured IDs remain explicit and are never guessed
from names. Incorrect IDs or Bluetooth permissions remain local troubleshooting
items, rather than fatal app startup conditions.

Snapshots show an independent waiting/connecting/connected/retry status per
sensor. Trainer samples are invalidated on degraded/disconnected/new-session
states; reconnect never makes a cached measurement current. HR freshness and
new-session invalidation remain in the standard HRS adapter. Hardware adapters
never acquire control or set targets, including reconnect and shutdown. This
read-only reconnect behavior is separate from the controlled workout recovery
implemented in M2; the latter still requires a safe target and explicit resume.

Hardware-free lifecycle tests exercise both arrival orders, neither device
present at startup, slow HR setup, individual setup failure, both disconnect
cases, silent HR and transient scan failure. They assert that the healthy peer
transport is preserved, old values do not become current, cleanup completes and
all FTMS write logs stay empty. Physical late-start/reconnect behavior on the
user's Mac remains a separate hardware observation, not a simulator claim.

## Explicit hardware workout GUI — 2026-10-20

At the user's request the GUI can now operate the real M2 trainer path. This is
an explicit software integration, not a claim that an agent performed a physical
load test or completed the remaining hardware acceptance record.

The official CLI requires --hardware --enable-control and an interactive exact
CONTROL <low bound> W confirmation before the web server/BLE startup. The default
bound is 50 W (lowered by maximum_w). Config files cannot enable control and no
unattended --yes exists. Without that flag, the previous read-only mode and its
zero-write guarantees remain unchanged. HTTP commands still require same Origin,
a strict action whitelist, an actual workout owner and server-side authorization;
a client cannot enable control by modifying a payload or UI label.

Discovery connects both sensors independently. An enabled hardware owner waits
for the trainer, then validates the workout/capabilities without Request Control.
HR setup never delays readiness. The first GUI Start checks fresh power/cadence,
requests control, sets a confirmed low initial target (<=50 W and maximum_w), and
starts only after FTMS success. Targets continue through SafetyController and the
sole TrainerControl writer. UI labels distinguish real ERG control from simulation.
Built-in low-power workouts, manual override and pause/STOP use the same engine.

Exactly one component owns each lifecycle/event stream. During RUNNING/PAUSED,
the idle trainer connector yields all trainer recovery to the engine; a shared
session lock prevents setup/start races. The engine's prepare(connect=False)
uses already discovered adapters and does not wait for/reconnect HR. The external
HR supervisor is its sole connection owner, so engine HR reconnect is disabled
in this runtime. One workout owner drains both event queues. Selecting a new
workout preserves the healthy HR connection and never starts resistance.

Trainer loss freezes time and invalidates control; recovery acknowledges a low
target and requires fresh telemetry and explicit GUI resume. The previous manual
high target is not restored. Resume is disabled while safe recovery is pending.
STOP bypasses the session lock and cancels pending/queued work. Failed Stop is
latched as a visible warning and error log, including on server shutdown; local
ABORTED is never presented as confirmed physical release. Starting another
session does not erase that warning. Idle connection supervision resumes only
when the engine is terminal, and an uncertain adapter must reconnect before a
new workout can be selected.

Automated hardware-path tests use fixed fake advertisements and MemoryTransport,
not physical devices. They verify no writes before Start, actual FTMS command
order/acknowledgements, absent/late HR, start cadence and low-power requirements,
manual/pause/resume/STOP through HTTP, shared browser state, exclusive reconnect,
unconfirmed Stop display and active-session shutdown. CLI tests verify exact
confirmation, refusal/cancellation/EOF/noninteractive paths before server startup.
The normal CI remains hardware-free. Real browser visual QA and physical load
response remain unverified by this agent; the existing hardware record is not
silently upgraded. Recording, SQLite and full M3 acceptance remain separate.
