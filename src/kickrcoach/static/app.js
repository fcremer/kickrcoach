const el = id => document.getElementById(id);
const states = {CONNECTED:'Verbunden',CONNECTING:'Verbindet …',DISCONNECTED:'Getrennt',DEGRADED:'Gestört'};
const freshness = {LIVE:'Live',STALE:'Veraltet',LOST:'Kein Signal'};
let lastMessage = 0;
let socket;
function clearReadings(message) {
  for (const id of ['power','cadence','hr']) el(id).textContent = '—';
  el('stream').textContent = message;
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
  el('mode').textContent = data.simulated?'SIMULATION':'BLUETOOTH · LIVE';
  el('error').hidden=!data.error; el('error').textContent=data.error||'';
  if(data.status!=='ready') {clearReadings(data.error?'Verbindung fehlgeschlagen':'Geräte werden gesucht und verbunden …');return;}
  el('stream').textContent='● Live-Verbindung';
  const seconds=Math.floor(data.elapsed_sec);
  el('elapsed').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
  for(const [id,value] of [['power',data.trainer.power_w],['cadence',data.trainer.cadence_rpm],['hr',data.hr.bpm]]) el(id).textContent=value===null?'—':Math.round(value);
  for(const role of ['trainer','hr']) {
    const source=data[role];
    el(`${role}-state`).textContent=states[source.connection]||source.connection;
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
