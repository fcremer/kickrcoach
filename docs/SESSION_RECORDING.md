# Local session recording and exports

This increment implements the requested recording + post-ride summary/TCX
slice of M3/M4. It does not declare either whole milestone complete.

## Use

Launch the web app with the existing command. Every GUI workout Start creates
one local session, including an armed WAITING start. Browsing telemetry without
starting a workout does not create a ride. The archive is independent of browser
connections and remains available after a server restart.

The dashboard's **Gespeicherte Trainings** panel offers:

- active workout duration, separate waiting/pause duration;
- time-weighted average/max actual power and HR, average cadence;
- valid-data coverage for each channel and sample/event counts;
- optional post-ride RPE (0–10), fan/cooling and notes;
- **TCX herunterladen** after a real ride ends;
- **JSON für Nachbetrachtung**, including full samples/events and feedback.

Archive default on macOS:
`~/Library/Application Support/KICKR Coach/sessions.sqlite3`.
Override with `--data-dir /path/to/archive` (or `KICKRCOACH_DATA_DIR`).
A process-level archive lease prevents a second server from incorrectly marking
another server's active session interrupted. The current app targets macOS;
the advisory file lock also works on Linux CI.

All data stays local; neither export contacts Strava or an AI service. For
Strava, download TCX and manually import at
[Strava's file uploader](https://www.strava.com/upload/select).
For AI review, attach JSON yourself: it contains the actual executed workout,
not merely its intended YAML. Earlier rides from before this feature cannot be
reconstructed from the former three-minute in-memory chart.

## Recording semantics

SQLite transactions persist samples approximately once per second, plus extra
points at workout events, and commit pending engine events after each command
or collector tick. Engine events retain their original monotonic timestamp;
sensor quality transitions record when the collector observes them (up to one
tick later). Sensor receive UTC and monotonic timestamps, ages, connection and
freshness states are retained alongside each sample. This is a sampled archive,
not a raw dump of every BLE notification.

Each sample includes workout block/index, workout elapsed time, planned target,
confirmed trainer target, manual override, actual power/cadence, HR, sensor
quality and the engine's diagnostic-drop counter. Events include target
acknowledgements, pause/resume, manual changes, safety/recovery and stop results.
The saved workout snapshot and configuration context exclude personal device
identifiers. HR boundaries and safety configuration are retained for analysis.

Persisted point UTC timestamps are derived from an initial UTC anchor plus
monotonic offsets. Changing the system clock during a workout does not reorder
activity points. Original sensor receive UTC remains available separately.
Workout elapsed time still uses the existing monotonic engine clock.

WAITING is archived but excluded from ride metrics/TCX. Paused samples and
missing/stale measurements are excluded from averages. Genuine live zero power
or cadence remains zero. Average weighting stops at the next sample, at a pause
boundary, or after one second, whichever comes first; collector gaps are never
interpolated. Coverage is an approximate represented duration, not proof of
continuous valid sensor reception. Zero coverage yields null averages.

Graceful shutdown attempts trainer stop first, then persists final events.
Unexpected termination leaves committed data intact; ACTIVE sessions become
INTERRUPTED at the next startup, with no automatic resume. Time after the last
committed point/event is unknown. Database work runs in worker threads; STOP
invalidates commands and attempts trainer stop before waiting for recording.
Cancellation joins an outstanding database write before finalization.

A write failure produces a visible warning and RECORDING_FAILED archive status
(best effort on disk, in-memory fallback while disk writes still fail). Already
committed data remains exportable as a partial record. Further new starts in
that server are blocked; existing trainer STOP remains available. Restart after
fixing storage. The app never reports a failed recording as a complete archive.

## Export contracts

JSON schema version 1 contains metadata, summary, samples and events with sample
and event counts that reconcile with SQLite. Feedback is nullable and never
inferred. Readiness input, CSV, target compliance and HR drift analysis remain
future work; this increment does not infer FTP, thresholds or training advice.

TCX uses Garmin Training Center Database v2 with ActivityExtension v2 Watts,
standard bike cadence and HeartRateBpm. Each trackpoint has UTC time. Points
come from RUNNING and, where elapsed time advanced, the final terminal sample.
Tracks split across pauses or collector gaps. Signal gaps omit the affected
field instead of substituting zero. No GPS is invented. Mandatory lap distance
and calorie aggregates are zero placeholders explicitly labelled as unrecorded.
TCX cannot retain our full safety/quality metadata; JSON is the authoritative
review export. Simulation and never-ridden starts cannot be exported to TCX.

Strava may calculate its own duration and averages. File-format compatibility
has been checked; actual Strava ingestion remains unverified until a user
uploads a real ride. No Strava account/API integration is included.

References: [Strava TCX requirements](https://developers.strava.com/docs/uploads/),
[Garmin TCX v2 schema](https://www8.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd),
[Garmin activity extension v2](https://www8.garmin.com/xmlschemas/ActivityExtensionv2.xsd).

## Verification

Deterministic memory-transport tests cover waiting, pause/resume, manual target
changes, completion/abort, stale HR exclusion, collection gaps, clock jumps,
restart recovery, browser independence, multiple sessions, feedback validation,
JSON/TCX endpoints and export restrictions. Failure tests inject SQLite errors
and block disk writes while verifying trainer STOP is not blocked.

A generated simulator-fixture TCX validated with `xmllint` against both official
Garmin schemas. Normal CI is offline with respect to BLE and uses parsed XML
assertions; it does not require downloaded schemas or Strava credentials.
Dashboard tests cover summary values, missing values, recording warnings and
TCX availability. Native visual review was unavailable because computer-use
permissions were pending; no visual or real-hardware acceptance is claimed.

## STOP result and TCX pause correction

Workout events now carry the trainer connection/control state and the actual
FTMS stop-confirmation flag at event time. A separate event after the stop
procedure captures its result. If the collector already persisted local ABORTED
while awaiting the indication, the recorder appends the subsequent final event
and snapshot instead of silently dropping them. Snapshot confirmation refers
to the current BLE connection; the immutable event preserves proof before a
shutdown disconnect clears that connection. An unused trainer is distinguished
from a missing stop confirmation. Neither file-write success nor lack of an
error is treated as proof of physical stop.

The summary exposes `trainer_stop_result` only where that evidence exists.
Older sessions are not retroactively labelled confirmed and are not modified.
TCX track splitting uses real pause state transitions and collection gaps,
rather than small wall/workout-clock differences due to FTMS indication delay.
The corrected exporter also works with existing samples/events and does not
require rewriting historical measurements. Tests reproduce a delayed Stop
while the collector records ABORTED, rejection/timeout, and the extra end-track
regression with nonzero protocol delay.
