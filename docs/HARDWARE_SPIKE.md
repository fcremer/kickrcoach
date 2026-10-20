# M0 hardware feasibility — Issue #1

Status: IMPLEMENTATION READY FOR HARDWARE TRIAL; M0 NOT ACCEPTED.
Date: 2026-10-20.

## Actual environment and findings

Implementation/test host: Linux 6.18.55 x86_64, glibc 2.39, Python 3.12.14.
Bleak 3.0.2 and pytest 8.4.2 installed successfully through the committed uv lock.
This host is not the target Mac and has no provided connection to the user's
KICKR CORE 2, iPhone or Apple Watch. No real BLE discovery, connection or power
command was attempted. CoreBluetooth behavior cannot be established here.
This is a missing hardware-validation environment, not evidence that HeartCast
or the KICKR is incompatible.

Automated evidence: 22 protocol/control/configuration/integration tests passed.
Tests cover fixed binary fixtures (including every truncation of a full optional
Indoor Bike Data packet), 8/16-bit HR, RR/contact, power limits, missing capability,
serialized control responses, mismatched/late responses, timeout/rejection,
control invalidation, cancelled procedures, no-BLE simulation, read-only no-write
behavior, cleanup after cancellation/failed Stop, cadence collapse, stale HR,
trainer disconnect and no automatic reconnect. A packaged CLI simulation also
completed Request Control → 50 W target → Start → acknowledged Stop/disconnect.
These results are software evidence only, not real-hardware acceptance.

## Hardware acceptance record

| Required observation | Actual result |
| --- | --- |
| Target Mac OS/Python | Not available; not tested |
| KICKR advertised name/platform identifier | Not observed; never commit stable identifier |
| HeartCast advertised name/HRS service | Not observed |
| Concurrent BLE connections | Not tested on hardware |
| GATT services/characteristics/properties | Not observed on hardware |
| Fitness Machine Feature/power-target capability | Not read on hardware |
| Supported power min/max/increment | Not read on hardware; target test refuses unknown range |
| Live KICKR power/cadence fields | Not received on hardware |
| Live HeartCast BPM/contact/receive timestamps | Not received on hardware |
| Notification frequencies/maximum gaps | Not measured on hardware |
| >=10 min continuous concurrent telemetry | Not performed |
| Request Control FTMS response | Not obtained on hardware |
| Confirmed low-power target/start responses | Not obtained on hardware |
| Physical/actual-power response to target | Not verified |
| Stop acknowledgement and load release | Not verified on hardware |
| Ctrl+C while controlling real trainer | Not verified on hardware |
| Disconnect/reconnect behavior | Simulated loss aborts; real behavior untested |

## Target-Mac procedure

1. Follow README installation/permissions and HeartCast broadcasting preparation.
2. Scan explicitly with `--hardware`; select the FTMS and HRS platform identifiers
   in ignored `config.local.toml`. Verify names locally; redact identifiers in any
   report. Missing advertised service is a blocker, not a reason to guess.
3. Run the 600-second read-only probe. Retain local output, review GATT/read
   failures, fields, invalid packets, notification intervals and HR freshness.
   Log sample counts/max gaps and whether either device disconnects. Check
   HeartCast with the iPhone screen locked and document observed behavior.
4. Run `--hardware --control-test` in a terminal; type the exact confirmation.
   Inspect feature/range validation, Request Control response, target/start
   responses, actual power/cadence and physical response at the configured low
   target. Record Stop response/load release. If any procedure fails, stop the
   trial and investigate; do not infer success from a completed BLE write.
5. Repeat the short controlled test with Ctrl+C. Record stop/disconnect outcomes.
   Test sensor/trainer loss under safe conditions; observe that there is no
   automatic load restore. A new invocation requires explicit control consent.
6. Replace pending entries with redacted actual results. Record deviations and
   unexplained disconnects. Only then assess Issue #1 acceptance.

## Deliberate limits and blockers

- M0 remains blocked on the physical macOS test. Issue #1 must remain open;
  do not start M1 or later milestones from simulator results.
- No workout logic, database, UI, adaptive control, physiological zone claims,
  HealthKit API or proprietary Wahoo command was added.
- Simulator supplies a BLE-shaped in-memory transport for this diagnostic; full
  production adapter protocols/state machines remain M1 work.
- Optional capability/read/subscription failures are logged and read-only
  probing continues. Missing required telemetry prevents successful completion;
  unavailable target-power support/range/control indications blocks control.
- A low-power target has a hard 100 W M0 ceiling, plus discovered bounds and
  increment checks. These engineering bounds are not an individual prescription.
- During this brief manual test, absent fresh HR/power/cadence or zero cadence
  aborts. This conservative experiment guard is not a workout safety policy or
  implementation of continuous HR chasing.
- No reconnect experiment automatically reapplies resistance. Disconnect/reset
  invalidates control and ends the probe. Investigate real CoreBluetooth and
  HeartCast constraints before designing M1 recovery behavior.
- FTMS has no dedicated Release Control procedure. An acknowledged Stop followed
  by disconnect is attempted; the application never restores an unknown previous
  load. A timeout/loss of radio/power/process kill cannot guarantee physical Stop.
- Receive timestamps are UTC plus monotonic clock; device-origin timestamps are
  unavailable in these measurements. Never interpret receive time as device time.
- Ten-minute summary checks do not replace field-specific/physical review.
  Simulation always labels itself and cannot pass real hardware acceptance.

## Protocol references

- Bluetooth SIG Fitness Machine Service 1.0.1:
  https://www.bluetooth.com/specifications/specs/fitness-machine-service-1-0-1/
- Bluetooth SIG Heart Rate Service:
  https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html
- Bleak CoreBluetooth/macOS behavior and permissions:
  https://bleak.readthedocs.io/en/latest/backends/macos.html

## Subsequent software work

On 2026-10-20 the user explicitly requested M1 despite this pending hardware gate.
M1 software work is documented separately in `M1_FOUNDATION.md`. The M0 results
above remain unchanged: no real-hardware acceptance was performed or inferred.

## User-reported Mac telemetry — 2026-10-20

The user confirmed the hardware probe works after supplying distinct trainer and
HR identifiers, and subsequently confirmed heart rate, power and cadence appear
in the local web dashboard. This establishes user-observed concurrent telemetry
through the real FTMS/HRS adapters into the browser. No identifiers or raw
physiological recordings were collected for this report.

This is user-reported evidence, not an agent-observed hardware capture. Exact
OS/Python versions, GATT properties, power range, notification intervals,
>=10-minute stability, FTMS control responses, physical target/Stop behavior and
hardware Ctrl+C/reconnect outcomes remain unverified. The original table records
the implementation-host spike; this update supersedes its telemetry-only pending
entries without implying the remaining acceptance criteria passed. Issue #1
remains open and M0 remains unaccepted.

M0/M1 and the read-only web preview are being integrated as software increments
at the user's request. Integration does not waive the hardware gate for actual
workout/control acceptance.
