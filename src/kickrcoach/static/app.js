import {validBoundaries, zoneFor, zoneRange} from './zones.mjs';
const el = id => document.getElementById(id);
const states = {CONNECTED:'Verbunden',CONNECTING:'Verbindet …',DISCONNECTED:'Getrennt',DEGRADED:'Gestört'};
const freshness = {LIVE:'Live',STALE:'Veraltet',LOST:'Kein Signal'};
let lastMessage = 0;
let socket;
let latest = null;
let busy = false;
const zoneColors = ['#8bbfde','#d5f77b','#f4d16d','#f6a16c','#ee8296'];
const workoutStates = {IDLE:'Bereit',PREPARING:'Wird vorbereitet …',READY:'Bereit zum Start',RUNNING:'Workout läuft',PAUSED:'Workout pausiert',COMPLETED:'Workout abgeschlossen',ABORTED:'Workout gestoppt'};
function time(value) {
  const seconds = Math.max(0,Math.ceil(value));
  return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
}
function renderZone(bpm, freshness, boundaries) {
  const configured = validBoundaries(boundaries);
  const zone = zoneFor(bpm,freshness,boundaries);
  el('hr-zone').textContent = zone ? `Zone ${zone}` : configured ? '—' : 'Zonen fehlen';
  el('zone-range').textContent = zone ? zoneRange(zone,boundaries) : configured ? 'Warte auf aktuellen Puls' : 'Bitte Zonengrenzen in der Konfiguration hinterlegen';
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
  const hardware=workout.mode==='hardware';
  el('workout-panel').setAttribute('aria-label',hardware?'Training am KICKR':'Simuliertes Workout');
  el('workout-mode').textContent=hardware?'WORKOUT · KICKR':'WORKOUT · SIMULATOR';
  el('workout-control-mode').textContent=hardware?'ECHTE TRAINERSTEUERUNG · ERG':'KEINE ECHTE TRAINERSTEUERUNG';
  el('start').textContent=hardware?'Training am KICKR starten':'Workout starten';
  el('workout-notice').hidden=!workout.notice;
  el('workout-notice').textContent=workout.notice||'';
  el('workout-state').textContent=workoutStates[state]||state;
  el('target').textContent=workout.target_w===null?'—':`${workout.target_w} W`;
  el('planned').textContent=workout.planned_target_w===null?'Noch kein Ziel bestätigt':`Blockvorgabe: ${workout.planned_target_w} W`;
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
  const canSelect=['READY','ABORTED','COMPLETED'].includes(state)&&online&&!busy;
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
    el('workout-error').textContent=error.message||'Server nicht erreichbar.'; el('workout-error').hidden=false;
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
  for (const id of ['trainer-state','hr-state']) {el(id).textContent='Unbekannt';el(id).dataset.live='false';}
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
  document.querySelector('.readonly').textContent=data.workout?(data.workout.mode==='hardware'?'ERG-Steuerung freigegeben · Start erfolgt manuell':'Workout-Steuerung ausschließlich simuliert'):'Nur Anzeige · keine Widerstandssteuerung';
  el('mode').textContent = data.simulated?'SIMULATION':'BLUETOOTH · LIVE';
  el('error').hidden=!data.error; el('error').textContent=data.error||'';
  if(data.status!=='ready') {clearReadings(data.error?'Verbindung fehlgeschlagen':'Geräte werden gesucht und verbunden …');return;}
  el('stream').textContent='● Live-Verbindung';
  renderZone(data.hr.bpm,data.hr.freshness,data.hr_zone_boundaries||[]);
  const seconds=Math.floor(data.elapsed_sec);
  el('elapsed').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  for(const [id,value] of [['power',data.trainer.power_w],['cadence',data.trainer.cadence_rpm],['hr',data.hr.bpm]]) el(id).textContent=value===null?'—':Math.round(value);
  for(const role of ['trainer','hr']) {
    const source=data[role];
    el(`${role}-state`).textContent=source.detail||states[source.connection]||source.connection;
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
