"""Local session archive. Missing measurements stay null; no trainer control."""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
import fcntl
from bisect import bisect_right
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET


@contextmanager
def archive_lease(directory: Path) -> Iterator[None]:
    """One server owns crash recovery for an archive (macOS/Linux)."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.recording.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Trainingsarchiv wird bereits von einer anderen App-Instanz verwendet.') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.failed_sessions: set[str] = set()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    status TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS samples_session ON samples(session_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                    data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
            ''')
            # A process restart never resumes a former controlled session.
            db.execute("UPDATE sessions SET status='INTERRUPTED' WHERE status='ACTIVE'")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, metadata: dict[str, Any], started_at: str) -> str:
        identifier = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                       (identifier, started_at, 'ACTIVE', json.dumps(metadata)))
        return identifier

    def append(self, identifier: str, events: list[dict], sample: dict | None, status: str) -> None:
        with self.connect() as db:
            db.executemany('INSERT INTO events(session_id,data) VALUES (?,?)',
                           [(identifier, json.dumps(event)) for event in events])
            if sample is not None:
                db.execute('INSERT INTO samples(session_id,data) VALUES (?,?)',
                           (identifier, json.dumps(sample)))
            db.execute('UPDATE sessions SET status=? WHERE id=?', (status, identifier))

    def record_failure(self, identifier: str) -> None:
        self.failed_sessions.add(identifier)
        try:
            with self.connect() as db:
                db.execute("UPDATE sessions SET status='RECORDING_FAILED' WHERE id=?", (identifier,))
        except (sqlite3.Error, OSError):
            # Disk may still be full. Keep a truthful in-memory status; on next
            # startup any remaining ACTIVE row becomes INTERRUPTED.
            pass

    def list_sessions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM sessions ORDER BY started_at DESC').fetchall()
        return [{'id':row['id'], 'started_at':row['started_at'], 'status':'RECORDING_FAILED' if row['id'] in self.failed_sessions else row['status'],
                 'name':json.loads(row['metadata'])['workout']['name'],
                 'simulated':json.loads(row['metadata'])['simulated']} for row in rows]

    def feedback(self, identifier: str, feedback: dict) -> None:
        with self.connect() as db:
            row = db.execute('SELECT metadata,status FROM sessions WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if row['status'] == 'ACTIVE':
                raise ValueError('Training zuerst beenden.')
            metadata = {**json.loads(row['metadata']), **feedback}
            db.execute('UPDATE sessions SET metadata=? WHERE id=?', (json.dumps(metadata), identifier))

    def detail(self, identifier: str) -> dict:
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            samples = [json.loads(item['data']) for item in db.execute(
                'SELECT data FROM samples WHERE session_id=? ORDER BY id', (identifier,))]
            events = [json.loads(item['data']) for item in db.execute(
                'SELECT data FROM events WHERE session_id=? ORDER BY id', (identifier,))]
        return {'schema_version':1, 'id':identifier, 'started_at':row['started_at'],
                'status':'RECORDING_FAILED' if identifier in self.failed_sessions else row['status'], 'metadata':json.loads(row['metadata']),
                'summary':summarize(samples, events), 'samples':samples, 'events':events}


def summarize(samples: list[dict], events: list[dict]) -> dict:
    """Time-weighted running metrics, limited to the next recorded second.

    Do not interpolate across collection gaps or treat missing values as zero.
    Terminal/event boundaries cap each sample's represented time.
    """
    end = max([p['offset_sec'] for p in samples] + [e['offset_sec'] for e in events] + [0])
    terminal_times = [event['offset_sec'] for event in events if event['state'] in ('COMPLETED','ABORTED')]
    if terminal_times:
        end = min(terminal_times)
    durations = {'WAITING':0.0, 'RUNNING':0.0, 'PAUSED':0.0}
    previous_state: str | None = None
    since = 0.0
    for event in sorted(events,key=lambda item:item['offset_sec']):
        if event['offset_sec'] > end:
            break
        state = event['state']
        if state not in (*durations,'COMPLETED','ABORTED') or state == previous_state:
            continue
        if previous_state in durations:
            durations[previous_state] += max(0,event['offset_sec']-since)
        previous_state, since = state, event['offset_sec']
    if previous_state in durations:
        durations[previous_state] += max(0,end-since)
    active = [p for p in samples if p['state'] == 'RUNNING']
    result: dict[str, Any] = {
        'sample_count':len(samples), 'event_count':len(events),
        'active_duration_sec':max([p['elapsed_sec'] for p in samples] +
                                  [e['elapsed_sec'] for e in events] + [0]),
        'recorded_duration_sec':end,
        'waiting_duration_sec':durations['WAITING'],
        'paused_duration_sec':durations['PAUSED'],
        'riding_wall_duration_sec':durations['RUNNING']+durations['PAUSED'],
    }
    pause_boundaries = sorted(e['offset_sec'] for e in events if e['state'] != 'RUNNING')
    for field in ('power_w', 'bpm', 'cadence_rpm'):
        total = weight = 0.0
        values = []
        for index, point in enumerate(samples):
            if point['state'] != 'RUNNING' or point[field] is None:
                continue
            boundary = samples[index+1]['offset_sec'] if index+1 < len(samples) else end
            position = bisect_right(pause_boundaries,point['offset_sec'])
            if position < len(pause_boundaries):
                boundary = min(boundary,pause_boundaries[position])
            duration = max(0, min(1, boundary-point['offset_sec']))
            values.append(point[field])
            total += point[field]*duration
            weight += duration
        result[f'average_{field}'] = round(total/weight, 1) if weight else None
        result[f'max_{field}'] = max(values) if values else None
        result[f'coverage_{field}_sec'] = round(weight, 2)
    result['running_sample_count'] = len(active)
    return result


class SessionRecorder:
    """One instance per workout owner, called serially by its async wrapper."""
    def __init__(self, store: SessionStore, *, simulated: bool, context: dict | None = None) -> None:
        self.store, self.simulated = store, simulated
        self.context = context or {}
        self.session_id: str | None = None
        self.active = False
        self.origin_mono = 0.0
        self.origin_utc = datetime.now(timezone.utc)
        self.last_sample = float('-inf')
        self.last_quality: tuple | None = None

    def update(self, *, now: float, utc: datetime, workout: dict, state: str,
               elapsed: float, events: list[dict], sample: dict) -> None:
        if not self.active:
            starts = [event for event in events if event['state'] in ('WAITING','RUNNING')]
            if not starts:
                if self.session_id and events and state in ('COMPLETED','ABORTED'):
                    late = [{**event, 'offset_sec':max(0,event['monotonic']-self.origin_mono),
                             'timestamp':(self.origin_utc+timedelta(seconds=max(0,event['monotonic']-self.origin_mono))).isoformat()} for event in events]
                    self.store.append(self.session_id, late, None, state)
                return
            self.origin_mono = min(event['monotonic'] for event in events)
            self.origin_utc = utc - timedelta(seconds=now-self.origin_mono)
            self.session_id = self.store.begin(
                {'workout':workout, 'simulated':self.simulated, **self.context}, self.origin_utc.isoformat())
            self.active = True
            self.last_sample = float('-inf')
            self.last_quality = None
        assert self.session_id is not None
        def stamped(mono: float) -> dict:
            offset = max(0, mono-self.origin_mono)
            return {'offset_sec':offset, 'timestamp':(self.origin_utc+timedelta(seconds=offset)).isoformat()}
        persisted = [{**event, **stamped(event['monotonic'])} for event in events
                     if event['monotonic'] >= self.origin_mono]
        quality = tuple(sample[key] for key in ('trainer_connection','trainer_freshness','hr_connection','hr_freshness'))
        if quality != self.last_quality:
            persisted.append({**stamped(now), 'state':state, 'elapsed_sec':elapsed,
                              'reason':'sensor_quality', 'quality':dict(zip(
                                  ('trainer_connection','trainer_freshness','hr_connection','hr_freshness'),quality))})
            self.last_quality = quality
        terminal = state in ('COMPLETED','ABORTED')
        point = None
        if now-self.last_sample >= 1 or terminal or events:
            point = {**sample, **stamped(now), 'state':state, 'elapsed_sec':elapsed}
            self.last_sample = now
        if persisted or point is not None or terminal:
            self.store.append(self.session_id, persisted, point, state if terminal else 'ACTIVE')
        if terminal:
            self.active = False


def export_tcx(session: dict) -> bytes:
    """TCX v2 with Garmin ActivityExtension v2 power; no invented GPS/distance."""
    if session['status'] == 'ACTIVE':
        raise ValueError('Training zuerst beenden.')
    if session['metadata']['simulated']:
        raise ValueError('Simulationsdaten werden nicht als Strava-Aktivität exportiert.')
    points = []
    previous_sample = None
    for point in session['samples']:
        if point['state'] == 'RUNNING' or (
            point['state'] in ('COMPLETED','ABORTED') and previous_sample is not None
            and previous_sample['state'] == 'RUNNING'
            and point['elapsed_sec'] > previous_sample['elapsed_sec']):
            points.append(point)
        previous_sample = point
    if not points or not any(p['power_w'] is not None or p['bpm'] is not None or p['cadence_rpm'] is not None for p in points):
        raise ValueError('Keine gefahrenen Messdaten für einen TCX-Export vorhanden.')
    ns = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'
    ext = 'http://www.garmin.com/xmlschemas/ActivityExtension/v2'
    ET.register_namespace('', ns)
    ET.register_namespace('ae', ext)
    def node(parent: ET.Element, tag: str, value: object | None = None, **attrs: str) -> ET.Element:
        child = ET.SubElement(parent, f'{{{ns}}}{tag}', attrs)
        if value is not None:
            child.text = str(value)
        return child
    root = ET.Element(f'{{{ns}}}TrainingCenterDatabase')
    activity = node(node(root, 'Activities'), 'Activity', Sport='Biking')
    started = next((event['timestamp'] for event in session['events'] if event['reason'] == 'started'), points[0]['timestamp'])
    node(activity, 'Id', started)
    lap = node(activity, 'Lap', StartTime=started)
    node(lap, 'TotalTimeSeconds', session['summary']['active_duration_sec'])
    # TCX requires these aggregates. Zero means unrecorded, not a speed estimate.
    node(lap, 'DistanceMeters', 0)
    node(lap, 'Calories', 0)
    node(lap, 'Intensity', 'Active')
    node(lap, 'TriggerMethod', 'Manual')
    track = None
    previous: dict | None = None
    for point in points:
        split = (previous is None or point['offset_sec']-previous['offset_sec'] > 2.5 or
                 point['offset_sec']-previous['offset_sec'] - (point['elapsed_sec']-previous['elapsed_sec']) > .01)
        if split:
            track = node(lap, 'Track')
        assert track is not None
        entry = node(track, 'Trackpoint')
        node(entry, 'Time', point['timestamp'])
        if point['bpm'] is not None:
            node(node(entry, 'HeartRateBpm'), 'Value', int(point['bpm']))
        if point['cadence_rpm'] is not None:
            node(entry, 'Cadence', min(254, max(0, round(point['cadence_rpm']))))
        if point['power_w'] is not None:
            extension = ET.SubElement(node(entry, 'Extensions'), f'{{{ext}}}TPX')
            ET.SubElement(extension, f'{{{ext}}}Watts').text = str(max(0, point['power_w']))
        previous = point
    node(activity, 'Notes', session['metadata']['workout']['name'] + '\nIndoor cycling · KICKR Coach. Distance/calories not recorded.')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
