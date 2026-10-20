import {validBoundaries, zoneFor, zoneRange} from './zones.mjs';
const el = id => document.getElementById(id);
const states = {CONNECTED:'Verbunden',CONNECTING:'Verbindet …',DISCONNECTED:'Getrennt',DEGRADED:'Gestört'};
const freshness = {LIVE:'Live',STALE:'Veraltet',LOST:'Kein Signal'};
let lastMessage = 0;
let socket;
let latest = null;
let busy = false;
const zoneColors = ['#8bbfde','#d5f77b','#f4d16d','#f6a16c','#ee8296'];
const workoutStates = {IDLE:'Bereit',PREPARING:'Wird vorbereitet …',READY:'Bereit zum Start',WAITING:'Warte aufs Losfahren',RUNNING:'Workout läuft',PAUSED:'Workout pausiert',COMPLETED:'Workout abgeschlossen',ABORTED:'Workout gestoppt'};
function time(value) {
  const seconds = Math.max(0,Math.ceil(value));
  return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
}
function renderZone(bpm, freshness, boundaries) {
  const configured = validBoundaries(boundaries);
  const zone = zoneFor(bpm,freshness,boundaries);
  el('hr-zone').textContent = zone ? `Zone ${zone}` : configured ? '—' : 'Zonen fehlen';
  el('zone-range').textContent = zone ? zoneRange(zone,boundaries) : configured ? 'Kein aktueller Puls' : 'Zonen nicht konfiguriert';
  el('zone-card').style.setProperty('--zone-color',zone ? zoneColors[zone-1] : '#efefea');
  for(const segment of el('zone-strip').children) segment.setAttribute('aria-current',String(Number(segment.dataset.zone)===zone));
  el('zone-ranges').replaceChildren();
  if(configured) for(let index=1;index<=5;index++) {
    const item=document.createElement('li'); item.textContent=`Zone ${index}: ${zoneRange(index,boundaries)}`;
    el('zone-ranges').append(item);
  }
}
function renderWorkout(workout, online=true) {
  el('workout-panel').hidden=!workout;
  if(!workout) return;
  const state=workout.state;
  el('catalog-errors').hidden=!workout.catalog_errors?.length;
  el('catalog-errors').textContent=(workout.catalog_errors||[]).join(' ');
  el('workout-duration').textContent=Number.isFinite(workout.duration_sec)?`${Math.round(workout.duration_sec/60)} min`:'';
  el('workout-notes').hidden=!workout.workout_notes;
  el('workout-notes').textContent=workout.workout_notes||'';
  el('block-notes').hidden=!workout.block_notes;
  el('block-notes').textContent=workout.block_notes||'';
  const previewKey=JSON.stringify([workout.selected,workout.preview]);
  if(el('workout-preview').dataset.key!==previewKey) {
    el('workout-preview').dataset.key=previewKey;
    el('workout-blocks').replaceChildren();
    for(const block of workout.preview||[]) {
      const row=document.createElement('tr');
      for(const value of [block.name,time(block.duration_sec),`${block.target_w} W`]) {
        const cell=document.createElement('td');cell.textContent=value;row.append(cell);
      }
      el('workout-blocks').append(row);
    }
  }
  const hardware=workout.mode==='hardware';
  el('workout-panel').setAttribute('aria-label',hardware?'Training am KICKR':'Simuliertes Workout');
  el('workout-mode').textContent=hardware?'WORKOUT · KICKR':'WORKOUT · SIMULATOR';
  el('workout-control-mode').textContent=hardware?'ECHTE TRAINERSTEUERUNG · ERG':'KEINE ECHTE TRAINERSTEUERUNG';
  el('start').textContent=hardware?'Training am KICKR starten':'Workout starten';
  el('workout-notice').hidden=!(workout.notice||workout.recording_error);
  el('workout-notice').textContent=[workout.notice,workout.recording_error].filter(Boolean).join(' ');
  el('workout-state').textContent=state==='IDLE'&&hardware?'Warte auf KICKR':workoutStates[state]||state;
  el('target').textContent=workout.target_w===null?'—':`${workout.target_w} W`;
  el('planned').textContent=workout.planned_target_w===null?'Noch kein Ziel bestätigt':`Blockvorgabe: ${workout.planned_target_w} W${workout.maximum_w ? ` · Limit: ${workout.maximum_w} W` : ''}`;
  el('block').textContent=workout.block;
  el('next-block').textContent=workout.next_block?`Danach: ${workout.next_block}`:'Danach: Workout-Ende';
  el('remaining').textContent=time(workout.remaining_sec);
  el('workout-elapsed').textContent=`Trainingszeit: ${time(workout.elapsed_sec)}`;
  el('override-info').hidden=workout.override_w===null;
  const select=el('workout-select');
  if(select.options.length!==workout.choices.length) {
    select.replaceChildren();
    for(const choice of workout.choices) {const option=document.createElement('option');option.value=choice.id;option.textContent=choice.name;select.append(option);}
  }
  if(select.dataset.selected!==workout.selected) {select.value=workout.selected;select.dataset.selected=workout.selected;}
  const canSelect=(workout.can_select??['IDLE','READY','ABORTED','COMPLETED'].includes(state))&&online&&!busy;
  select.disabled=!canSelect; el('workout-load').disabled=!canSelect;
  for(const [id,allowed] of Object.entries({start:state==='READY'&&workout.can_start!==false,pause:state==='RUNNING',resume:state==='PAUSED'&&workout.can_resume!==false,increase:state==='RUNNING',decrease:state==='RUNNING'})) el(id).disabled=!allowed||busy||!online;
  el('stop').disabled=['IDLE','ABORTED','COMPLETED'].includes(state);
  if(!online) el('workout-state').textContent='Serverdaten fehlen · Zustand unbekannt';
}
async function command(action) {
  if(busy&&action!=='stop') return;
  busy=true; el('workout-error').hidden=true;
  if(latest) renderWorkout(latest.workout);
  try {
    const response=await fetch('/api/workout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...(action==='select'?{selected:el('workout-select').value}:{})})});
    const result=await response.json();
    if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Befehl konnte nicht ausgeführt werden.');
    if(latest) {latest.workout=result;renderWorkout(result);}
  } catch(error) {
    el('workout-error').textContent=action==='stop'?'STOP konnte nicht bestätigt werden. Treten beenden und Trainer entlasten.':(error.message||'Server nicht erreichbar.'); el('workout-error').hidden=false;
  } finally {busy=false;if(latest) renderWorkout(latest.workout,lastMessage>0&&performance.now()-lastMessage<3500);}
}
for(const button of document.querySelectorAll('[data-action]')) button.addEventListener('click',()=>command(button.dataset.action));
el('workout-load').addEventListener('click',()=>command('select'));
el('workout-select').addEventListener('change',()=>command('select'));

function clearReadings(message) {
  for (const id of ['power','cadence','hr']) el(id).textContent = '—';
  el('stream').textContent = message;
  renderZone(null,'LOST',latest?.hr_zone_boundaries||[]);
  if(latest) renderWorkout(latest.workout,false);
  for (const id of ['trainer-fresh','hr-fresh']) el(id).textContent = 'Keine aktuellen Daten';
  for (const id of ['trainer-state','hr-state']) {el(id).textContent='Unbekannt';el(id).dataset.live='false';el(id).title='Keine aktuellen Serverdaten';}
  for (const id of ['trainer-age','hr-age']) el(id).textContent='—';
}
function chart(id, points, field, unit, now) {
  const values = points.map(p=>p[field]).filter(v=>v!==null);
  const max = Math.max(1,...values)*1.15;
  let path = '', drawing = false, previous = null;
  for (const p of points) {
    if (p[field]===null || now-p.t>180) {drawing=false;continue;}
    if (previous!==null && p.t-previous>2.5) drawing=false;
    const x = Math.max(0,900*(1-(now-p.t)/180)), y=85-80*p[field]/max;
    path += `${drawing?'L':'M'}${x.toFixed(1)},${y.toFixed(1)} `;
    drawing=true; previous=p.t;
  }
  el(`${id}-chart`).querySelector('path').setAttribute('d',path);
  el(`${id}-scale`).textContent=`0–${Math.ceil(max)} ${unit}`;
}
function render(data) {
  latest=data;
  renderWorkout(data.workout||null);
  document.querySelector('.readonly').textContent=data.workout?(data.workout.mode==='hardware'?'ERG freigegeben':'Simulierte Steuerung'):'Nur Anzeige';
  el('mode').textContent = data.simulated?'SIMULATION':'BLUETOOTH';
  el('error').hidden=!data.error; el('error').textContent=data.error||'';
  if(data.status!=='ready') {clearReadings(data.error?'Verbindung fehlgeschlagen':'Geräte werden gesucht und verbunden …');return;}
  el('stream').textContent='Live';
  renderZone(data.hr.bpm,data.hr.freshness,data.hr_zone_boundaries||[]);
  el('clock-label').textContent=data.workout?'TRAININGSZEIT':'BEOBACHTUNGSZEIT';
  const seconds=Math.floor(data.workout?data.workout.elapsed_sec:data.elapsed_sec);
  el('elapsed').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  for(const [id,value] of [['power',data.trainer.power_w],['cadence',data.trainer.cadence_rpm],['hr',data.hr.bpm]]) el(id).textContent=value===null?'—':Math.round(value);
  for(const role of ['trainer','hr']) {
    const source=data[role];
    el(`${role}-state`).textContent=source.connection==='CONNECTED'?'Verbunden':'Nicht verbunden';
    el(`${role}-state`).title=source.detail||states[source.connection]||source.connection;
    el(`${role}-state`).dataset.live=source.connection==='CONNECTED';
    el(`${role}-fresh`).textContent=freshness[source.freshness]||source.freshness;
    el(`${role}-age`).textContent=source.age_sec===null?'Noch kein Messwert':`vor ${Math.floor(source.age_sec)} s`;
  }
  chart('power',data.history,'power','W',data.elapsed_sec);
  chart('cadence',data.history,'cadence','rpm',data.elapsed_sec);
  chart('hr',data.history,'hr','bpm',data.elapsed_sec);
  el('chart-empty').hidden=data.history.some(p=>p.power!==null||p.hr!==null||p.cadence!==null);
}
function connect() {
  socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/telemetry`);
  socket.onmessage=event=>{lastMessage=performance.now();render(JSON.parse(event.data));};
  socket.onclose=()=>{lastMessage=0;clearReadings('Verbindung unterbrochen · verbinde erneut …');setTimeout(connect,2000);};
  socket.onerror=()=>socket.close();
}
setInterval(()=>{if(!lastMessage||performance.now()-lastMessage>3500) clearReadings('Keine aktuellen Serverdaten · warte auf Verbindung …');},1000);
connect();

let selectedSessionId = null;
let feedbackSessionId = null;
let latestRecordedSessionId = null;
let archiveBusy = false;
const archiveStates = {ACTIVE:'Aufzeichnung läuft',COMPLETED:'Abgeschlossen',ABORTED:'Gestoppt',INTERRUPTED:'Unterbrochen · App wurde unerwartet beendet',RECORDING_FAILED:'Aufzeichnungsfehler · Daten unvollständig'};
function renderReview(session) {
  const summary=session.summary;
  el('session-feedback').hidden=session.status==='ACTIVE';
  if(feedbackSessionId!==session.id) {
    el('feedback-rpe').value=session.metadata.rpe??'';
    el('feedback-cooling').value=session.metadata.cooling===null||session.metadata.cooling===undefined?'':session.metadata.cooling?'yes':'no';
    el('feedback-notes').value=session.metadata.notes||'';
    el('feedback-status').textContent='';
    feedbackSessionId=session.id;
  }
  const value=(number,unit)=>number===null||number===undefined?'—':`${number} ${unit}`;
  el('session-review').hidden=false;
  el('review-name').textContent=session.metadata.workout.name;
  el('review-status').textContent=`${session.metadata.simulated?'Simulation · ':''}${archiveStates[session.status]||session.status} · ${new Date(session.started_at).toLocaleString('de-DE')}`;
  el('review-duration').textContent=time(summary.active_duration_sec);
  el('review-timing').textContent=`Pausen: ${time(summary.paused_duration_sec||0)} · Wartezeit vor dem Losfahren: ${time(summary.waiting_duration_sec||0)}`;
  el('review-power').textContent=`${value(summary.average_power_w,'W')} / ${value(summary.max_power_w,'W')}`;
  el('review-hr').textContent=`${value(summary.average_bpm,'bpm')} / ${value(summary.max_bpm,'bpm')}`;
  el('review-cadence').textContent=value(summary.average_cadence_rpm,'rpm');
  el('review-quality').textContent=`Abdeckung · Leistung ${time(summary.coverage_power_w_sec)} · Puls ${time(summary.coverage_bpm_sec)} · Kadenz ${time(summary.coverage_cadence_rpm_sec)} · ${summary.sample_count} Messpunkte · ${summary.event_count} Ereignisse`;
  const canExport=session.status!=='ACTIVE'&&!session.metadata.simulated&&summary.running_sample_count>0&&[summary.max_power_w,summary.max_bpm,summary.max_cadence_rpm].some(value=>value!==null&&value!==undefined);
  el('download-tcx').hidden=!canExport;
  el('download-tcx').href=`/api/sessions/${encodeURIComponent(session.id)}/download/tcx`;
  el('download-json').href=`/api/sessions/${encodeURIComponent(session.id)}/download/json`;
  el('review-export-info').textContent=session.metadata.simulated?'Simulation · nur JSON':session.status==='ACTIVE'?'TCX nach Trainingsende':canExport?'TCX · Leistung, Puls, Kadenz · ohne Distanz / Kalorien':'TCX nicht verfügbar · keine gefahrenen Messdaten';
}
async function refreshSessions() {
  if(archiveBusy) return;
  archiveBusy=true;
  try {
    const response=await fetch('/api/sessions');
    if(!response.ok) throw new Error('Archiv konnte nicht geladen werden.');
    const sessions=await response.json();
    el('session-list').replaceChildren();
    if(!sessions.length) {const empty=document.createElement('p');empty.textContent='Noch keine Trainings gespeichert.';el('session-list').append(empty);el('session-review').hidden=true;}
    if(latest?.workout?.session_id&&latest.workout.session_id!==latestRecordedSessionId) {selectedSessionId=latest.workout.session_id;latestRecordedSessionId=selectedSessionId;}
    if(!sessions.some(session=>session.id===selectedSessionId)) selectedSessionId=sessions[0]?.id||null;
    for(const session of sessions) {
      const button=document.createElement('button');button.type='button';
      button.textContent=`${new Date(session.started_at).toLocaleString('de-DE')} · ${session.name} · ${session.simulated?'Simulation · ':''}${archiveStates[session.status]||session.status}`;
      button.setAttribute('aria-pressed',String(session.id===selectedSessionId));
      button.addEventListener('click',()=>{if(archiveBusy)return;selectedSessionId=session.id;refreshSessions();});
      el('session-list').append(button);
    }
    if(selectedSessionId) {
      const detail=await fetch(`/api/sessions/${encodeURIComponent(selectedSessionId)}`);
      if(!detail.ok) throw new Error('Training konnte nicht geladen werden.');
      renderReview(await detail.json());
    }
    el('archive-error').hidden=true;
  } catch(error) {el('archive-error').textContent=error.message||'Trainingsarchiv nicht erreichbar.';el('archive-error').hidden=false;}
  finally {archiveBusy=false;}
}
el('sessions-refresh').addEventListener('click',refreshSessions);
setInterval(refreshSessions,5000);
refreshSessions();

el('session-feedback').addEventListener('submit',async event=>{
  event.preventDefault();
  if(!selectedSessionId||archiveBusy||feedbackSessionId!==selectedSessionId) return;
  const identifier=selectedSessionId;
  el('feedback-save').disabled=true;
  try {
    const response=await fetch(`/api/sessions/${encodeURIComponent(identifier)}/feedback`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        rpe:el('feedback-rpe').value===''?null:Number(el('feedback-rpe').value),
        cooling:el('feedback-cooling').value===''?null:el('feedback-cooling').value==='yes',
        notes:el('feedback-notes').value||null})});
    if(!response.ok) throw new Error('Rückmeldung konnte nicht gespeichert werden.');
    if(identifier===selectedSessionId) el('feedback-status').textContent='Rückmeldung gespeichert · auch im JSON enthalten.';
  } catch(error) {el('feedback-status').textContent=error.message;}
  finally {el('feedback-save').disabled=false;}
});
