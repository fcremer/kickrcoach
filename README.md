# KICKR Coach — local training dashboard

## Training über die Webansicht steuern

```sh
uv run --locked kickrcoach-web --hardware --enable-control --config config.toml
```

Der Server startet direkt, ohne Texteingabe oder TTY-Anforderung.
`--enable-control` gibt die GUI-Steuerung ausdrücklich frei. Bis zum Klick auf
**Training am KICKR starten** bleiben Verbindung und Workout-Vorbereitung lesend.
Der separate M0-Diagnosetest behält seine interaktive Bestätigung.

Unter **http://127.0.0.1:8000** kannst du bereits ohne KICKR-Verbindung ein
Workout auswählen und den Ablauf ansehen. Die Auswahl bleibt beim Verbinden
erhalten. Nach erfolgreicher Trainerprüfung **Training starten** drücken. Du kannst danach aufsteigen und losfahren. Ohne frische Pedaldaten zeigt
die GUI **Warte aufs Losfahren**: keine Zeit läuft und es werden noch keine
Steuerbefehle gesendet. Erst beim Losfahren beginnt der erste Block. STOP bricht
auch einen solchen wartenden Start ab.
Zielleistung, aktueller/nächster Block und Restzeit erscheinen in der GUI.
Pause, Fortsetzen, ±5 W und STOP steuern den echten Trainer über dieselbe
M2-Sicherheitslogik wie im Simulator. Das 4-Minuten-Workout beginnt bei 40 W;
Starts bleiben auf höchstens 50 W begrenzt. Für spätere Ziele gilt
`training_maximum_w` (Standard: 300 W), zusätzlich zum am Trainer erkannten
Leistungsbereich und dessen Schrittweite. Dieses Limit ist eine Obergrenze,
kein automatisch eingestellter Trainingswert. `maximum_w` ist ausschließlich
die weiterhin höchstens 100 W große Grenze des M0-Diagnosetests. Ein fehlender
Pulssensor blockiert statisches Training nicht; erst die tatsächliche
Widerstandssteuerung benötigt frische Trainerleistung und sichere Kadenz.

Nach Trainerverlust pausiert die Zeit. Die App verbindet sich mit niedrigem
Ziel neu, startet aber erst nach **Fortsetzen**. Ein nicht bestätigter STOP wird
sichtbar gemeldet und im Terminal protokolliert. Neuladen des Browsers setzt
weder Workout noch Freigabe zurück. Ctrl+C versucht Stop und trennt beide Geräte.
Ohne `--enable-control` bleibt der Hardware-Modus ausschließlich lesend.

## Eigene YAML-Workouts

Beim Serverstart werden zusätzlich alle `.yaml`/`.yml`-Dateien aus `./workouts`
geladen. In der GUI auswählen; **Trainingsablauf** zeigt Blöcke, Dauer und
Wattvorgaben. Workout- und aktuelle Blockhinweise erscheinen ebenfalls.
Neue/geänderte Dateien benötigen einen Serverneustart. **Neu laden** setzt nur
die aktuell ausgewählte Einheit zurück; eine laufende Einheit wird nie durch
Dateiänderungen verändert. Ein anderer Ordner ist mit `--workouts-dir` wählbar.

Die GUI akzeptiert hier absolute Wattvorgaben, den ersten Block bis 50 W und
spätere Ziele bis `training_maximum_w`. Ungültige Dateien werden mit einem
sichtbaren Hinweis übersprungen; Trainerfähigkeiten werden bei der Vorbereitung
zusätzlich geprüft. Hinweise wie „bei RPE über 5 nicht steigern“ sind Text:
Die Blöcke laufen automatisch. Bei Bedarf selbst reduzieren, pausieren oder
stoppen. Der tatsächlich gefahrene Workout-Snapshot wird im Archiv gespeichert.

## Kadenz und Zielanzeigen

`cadence.minimum_rpm` / `cadence.maximum_rpm` definieren die Kadenz pro Block.
Ohne diese Angaben gibt es keine Kadenzvorgabe. Die Kacheln zeigen während des
Trainings **unter Ziel / im Ziel / über Ziel**, ergänzt um **schneller treten /
Kadenz halten / langsamer treten** bei der Kadenz. Watt werden mit sichtbarer
±5-W-Anzeigetoleranz bewertet; die Leistungsregelung bleibt beim KICKR.
Veraltete/fehlende Daten erhalten keine Zielbewertung.

Der [ChatGPT-Planungsworkflow](docs/TRAINING_PLANNER.md) beschreibt die Übergabe
von historischem Kontext, YAML-Plänen und tatsächlichen JSON-Trainingsdaten.

## Trainings speichern und exportieren

GUI-Trainings werden jetzt automatisch lokal aufgezeichnet. Unter
**Gespeicherte Trainings** findest du nach dem Training eine Zusammenfassung,
Messwertabdeckung sowie **TCX herunterladen** für den manuellen Strava-Import.
**JSON für Nachbetrachtung** enthält außerdem Soll-/Ist-Werte, Messwertqualität,
Pausen, Sicherheitsereignisse und den Workout-Snapshot für deine KI-Auswertung.
RPE, Kühlung und Notizen kannst du nach dem Training ergänzen.

Das Archiv bleibt bei Browser- und App-Neustarts erhalten. Standard auf macOS:
`~/Library/Application Support/KICKR Coach/sessions.sqlite3`; mit `--data-dir`
änderbar. Simulationen sind deutlich markiert und nur als JSON exportierbar.
Frühere Trainings ohne Aufzeichnung lassen sich nicht nachträglich herstellen.
Details und bekannte Grenzen: [Session recording](docs/SESSION_RECORDING.md).

## M2: Workout-Engine im Simulator

```sh
uv sync --locked --extra dev
uv run --locked kickrcoach-workout workouts/baseline.yaml
```

Die validierte YAML-Datei läuft mit virtueller Zeit durch alle ERG-Blöcke.
Die Engine unterstützt Pause/Fortsetzen, -5/+5 W pro aktuellem Block, STOP,
Kadenz-/Telemetrie-Schutz und sichere Wiederverbindung. Der neue Startbefehl
ist ausschließlich simuliert. Die Webansicht kann dieselbe Engine nach der
oben beschriebenen expliziten Freigabe am echten Trainer verwenden.

Fehlerfälle lassen sich mit `--scenario cadence-collapse`, `disconnect`,
`stale-hr`, `timeout` oder `rejected-control` auslösen. Statisches Training läuft
bei Pulsausfall weiter; nach Trainerverlust wird kein Training automatisch
fortgesetzt. Details, API, Sicherheitsgrenzen und Abnahme:
[docs/M2_WORKOUT_ENGINE.md](docs/M2_WORKOUT_ENGINE.md).

## Webansicht starten

Die erste Webversion zeigt Leistung, Trittfrequenz, Puls, Messwertalter,
Geräteverbindungen und einen flüchtigen 3-Minuten-Verlauf. Mit dem folgenden
Startbefehl bleibt sie ausschließlich lesend und sendet keine Steuerbefehle:

```sh
uv sync --locked --extra dev
# Mit echten Geräten; vorhandene trainer_id UND hr_id in config.toml verwenden:
uv run --locked kickrcoach-web --hardware --config config.toml
```

Dann **http://127.0.0.1:8000** öffnen. Den bisherigen CLI-Probe vorher beenden;
Bluetooth einschalten, HeartCast-Broadcast starten und andere Trainer-Apps schließen.
Die Bluetooth-Berechtigung gilt weiterhin für das ausführende Terminal.
Mit **Ctrl+C im Terminal** werden beide Geräte getrennt und der Server beendet.
Bei belegtem Port z. B. `--port 8001` ergänzen.

Ohne Hardware-Flag startet eine deutlich markierte Simulation mit Workout-Auswahl,
Start, Pause, Fortsetzen, ±5 W und STOP:

```sh
uv run --locked kickrcoach-web --config config.toml
```

Die Pulszone ist die Hauptanzeige; die BPM-Zahl steht kleiner darunter. Deine
vier Übergangsgrenzen stehen als `hr_zone_boundaries` in der lokalen TOML-Datei:
die ersten BPM-Werte von Z2, Z3, Z4 und Z5, strikt aufsteigend. Z1 umfasst alle
Werte darunter. Bei Änderungen die App neu starten. Ohne Grenzen wird keine Zone
geschätzt; bei veraltetem/fehlendem Puls wird auch die aktuelle Zone ausgeblendet.
`config.example.toml` enthält bewusst keine persönlichen Zonengrenzen.

Ein Serverprozess besitzt die Geräteverbindungen; mehrere Browser oder ein
Neuladen der Seite teilen sich dieselbe Erfassung. Trainer und Pulssensor werden unabhängig gesucht und verbunden. Sie dürfen erst
nach dem App-Start eingeschaltet werden. Fehlt ein Gerät oder bricht seine
Verbindung ab, versucht die App es automatisch erneut; die andere Verbindung
bleibt bestehen. Ein ausgebliebener Puls wird als veraltet/fehlend markiert und
die HR-Verbindung bei längerem Ausfall separat neu aufgebaut. Der Browser
verbindet seinen Datenstrom ebenfalls automatisch neu.
Alte Messwerte werden ausgeblendet; 0 W bzw. 0 rpm bleiben gültige Messwerte.

Der Server bindet ausschließlich an `127.0.0.1`, nicht ans LAN. Keine externen
Schriften, CDNs, Cloud-Aufrufe oder dauerhafte Speicherung. Der vorgezogene
UI-Ausschnitt schließt M0/M3 nicht ab. Details und offene Abnahmen:
[docs/WEB_DASHBOARD.md](docs/WEB_DASHBOARD.md).

---

## M0/M1 foundation

The original M0 command implements the diagnostic software for Issue #1. It scans/probes standard
Bluetooth FTMS trainers and HRS heart-rate sources, including the intended
KICKR CORE 2 + HeartCast path. Hardware compatibility is **not yet proven**.
M1 adds shared trainer/HR contracts and deterministic simulators. Workouts, web UI,
database, analytics and adaptation are not included. Real hardware acceptance remains pending.


## M1: shared adapters and deterministic simulation

M1 was requested explicitly after the M0 draft. This is a stacked change based on
PR #2; it does not declare M0 complete, merge that PR or close Issue #1.
See [docs/M1_FOUNDATION.md](docs/M1_FOUNDATION.md) for contracts, state transitions,
acceptance mapping and limitations.

```sh
uv sync --locked --extra dev
uv run --locked pytest -q
uv run --locked kickrcoach-adapters --steps 20
uv run --locked kickrcoach-adapters --scenario stale-hr --steps 20
uv run --locked kickrcoach-adapters --scenario rejected-control
```

The adapter demo always runs in memory, using virtual one-second steps. It
opens no Bluetooth connection. Timeout, rejected-control and disconnect scenarios
exit with code 1; stale HR/cadence collapse remain observable telemetry and do
not implement M2 workout policy. All output is labelled `simulated`.

Both simulator and real adapters satisfy the same runtime-checkable protocols:

```python
from kickrcoach.domain import TrainerAdapter, HeartRateSource
from kickrcoach.simulation import ManualClock, SimulatedTrainer, SimulatedHeartRateSource

# Inside an async function:
clock = ManualClock()
trainer: TrainerAdapter = SimulatedTrainer(clock=clock)
hr: HeartRateSource = SimulatedHeartRateSource(clock=clock)
await trainer.connect()               # read-only; no automatic control
await hr.connect()
try:
    await trainer.request_control()  # verified FTMS response
    await trainer.set_target_power(50)
    await trainer.resume()           # requires a newly acknowledged target
    # Consume immutable events from trainer.events / hr.events.
finally:
    await trainer.disconnect()       # attempts stop, even after command failure
    await hr.disconnect()
```

For real hardware, explicitly select previously discovered device objects using
`ble.discover()` / `ble.select()`, wrap each with `ble.transport_factory(device)`,
and construct `trainer.FtmsTrainer(factory)` / `heart_rate.BleHeartRateSource(factory)`.
The library API does not prompt; the calling application is responsible for
explicit user authorization before requesting control. The M0 CLI still enforces
its interactive hardware-control confirmation and remains the acceptance path.
Standalone adapters retain their conservative default 100 W policy. The GUI
supplies its separate configured training_maximum_w; the M0 probe keeps its
independent hard 100 W diagnostic ceiling. HR loss does not change trainer power in M1.

## M0: install and test

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for the locked workflow:

```sh
uv sync --locked --extra dev
uv run --locked pytest -q
uv run --locked kickrcoach probe --duration-sec 3
uv run --locked kickrcoach probe --duration-sec 4 --control-test
```

The default is simulation. It does not import Bleak's backend, scan Bluetooth or
command any physical device. `python -m kickrcoach` is also supported after install.
An alternative is `python -m pip install '.[dev]'` (direct dependencies pinned;
use uv for the complete platform-aware lock).

Simulation fault probes:

```sh
uv run kickrcoach probe --duration-sec 5 --scenario disconnect
uv run kickrcoach probe --duration-sec 5 --control-test --scenario timeout
uv run kickrcoach probe --duration-sec 5 --control-test --scenario rejected-control
uv run kickrcoach probe --duration-sec 6 --control-test --scenario cadence-collapse
```

`stale-hr` stops sending HR after two samples. Run longer than 17 seconds to see
LIVE → STALE → LOST. For a manual test longer than the default 10 seconds,
use `test_sec` in a local config (maximum 30 seconds). HR is never used to chase
a training zone. Loss during this short diagnostic control experiment aborts
that experiment; this is not the later workout policy in SPEC FR-014.
Fault scenarios are refused with `--hardware`.

## On the target Mac

Enable Bluetooth and allow your terminal/IDE under System Settings → Privacy &
Security → Bluetooth. Bleak uses CoreBluetooth identifiers (UUIDs), not MAC
addresses on macOS. Start HeartCast on Apple Watch **and iPhone**, check that the
watch HR reaches the phone, and enable its BLE broadcast. No Python HealthKit
access or private HeartCast API is assumed. A standard BLE chest strap can replace it.
Keep the phone nearby and verify broadcast continuity/screen-lock behavior during
M0. Close MyWhoosh, Wahoo, Zwift or other apps that could own trainer control.
Use the bike with its motor off and an appropriate, checked trainer setup.

```sh
uv run kickrcoach scan --hardware
cp config.example.toml config.local.toml
# Enter trainer_id and hr_id from this Mac's scan in config.local.toml.
uv run kickrcoach probe --hardware --config config.local.toml --duration-sec 600
```

Scan lists local names, platform identifiers and service UUIDs. Selection requires
explicit identifiers and the advertised FTMS/HRS service. If a device omits its
service advertisement, record that blocker rather than guessing by its name.
Normal probing only reads/subscribes: **no Request Control, target, start or stop
writes occur**. Optional feature/range/status/control capabilities degrade with
diagnostic events; power/cadence/HR availability is still required for acceptance.

The probe logs GATT properties, feature bit masks and supported power range,
decoded power/cadence/HR, UTC receive timestamps, monotonic receive times,
notification intervals, freshness and disconnects. Missing packet fields are not
silently copied forward. No source timestamp is invented when BLE has none.

## Deliberate low-power control experiment

```sh
uv run kickrcoach probe --hardware --config config.local.toml --control-test --target-w 50
```

An interactive TTY is required. Type the exact displayed `CONTROL 50 W` phrase
before Bluetooth is opened. There is no unattended `--yes` switch; configuration
cannot enable hardware or control. Prepare to pedal before running the command.
The probe waits up to `lost_sec` for live HR, power and positive cadence before
requesting control. Target power must match the discovered range/increment and
the configured bound, with an absolute M0 ceiling of 100 W. Unknown range or
unconfirmed power-target support blocks the test.

The sole control writer subscribes to indications, serializes procedures and
checks `0x80 / requested opcode / result` responses. A BLE write alone is never
success. The sequence is Request Control → Set Target Power → Start/Resume →
bounded observation (default 10 s) → Stop. Observe the load change yourself and
inspect actual KICKR power/cadence; protocol acknowledgement alone is not physical
verification. Never infer FTP or an indoor training zone from this experiment.

Ctrl+C cancels the probe and attempts an acknowledged Stop if control was
attempted, then disconnects both devices. Reset/control-permission loss,
command timeout/rejection, disconnected sensors, and stale/zero-cadence telemetry
during the manual test abort it. There is no auto-reconnect or high-load restore.
FTMS has no separate Release Control command: stop and disconnect are used.
If Stop fails, the output explicitly requires a physical stop/unload; a software
shutdown cannot guarantee the trainer has released resistance. Do not restore an
unknown previous target. Discovery/read-only cancellation never sends Stop.

## Evidence and exit codes

- `0`: short smoke probe completed with fresh power/cadence/HR, or the telemetry
  checks for a >=600 s run passed. This does **not** declare full M0 acceptance.
- `1`: configuration, transport, control or telemetry check failed.
- `2`: confirmation cancelled; `130`: Ctrl+C.

Output is JSON lines on stdout; control diagnostics are on stderr. For a read-only
run you can capture evidence locally:

```sh
uv run kickrcoach probe --hardware --config config.local.toml > hardware.local.jsonl 2> hardware.local.log
```

Do not pipe/redirect an interactive control run's stdout: the confirmation must
remain visible. Run it in the terminal and retain only reviewed, redacted results.
Device identifiers/names appear in scan output; HR logs are personal data. Local
config, JSONL and log files are ignored. Do not commit raw captures.
Summaries include sample counts, maximum notification gaps, invalid-packet counts
and whether 10-minute concurrent telemetry was observed. Review the raw evidence
for field-specific dropouts and physically verify the low-power test.
Update [docs/HARDWARE_SPIKE.md](docs/HARDWARE_SPIKE.md) with actual Mac findings;
Issue #1 stays open until the real acceptance checklist is satisfied.

## Components and constraints

- `protocol.py`: pure, fixture-tested FTMS/HRS decoding.
- `ble.py`: explicit discovery/selection and Bleak boundary.
- `control.py`, `safety.py`: one serialized control path and M0 target validation.
- `sensors.py`: receive-time HR freshness (default 5 s stale / 15 s lost).
- `probe.py`: concurrent M0 orchestration, diagnostic output and cleanup.
- `simulator.py`: in-memory transport with fault scenarios.
- `cli.py`: validated TOML/CLI configuration and explicit hardware consent.

The M0 transport simulator remains a diagnostic test double. M1 adapters live in
`trainer.py`, `heart_rate.py`, `domain.py`, `transport.py` and `simulation.py`.
Neither simulator establishes CORE 2/HeartCast hardware compatibility.
See [SPEC.md](SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[AGENTS.md](AGENTS.md) and [MILESTONES.md](MILESTONES.md).

Protocol references: [Bluetooth SIG FTMS](https://www.bluetooth.com/specifications/specs/fitness-machine-service-1-0-1/),
[Bluetooth SIG HRS](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/HRS_v1.0/out/en/index-en.html),
[Bleak macOS backend](https://bleak.readthedocs.io/en/latest/backends/macos.html).
