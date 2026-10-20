import test from 'node:test';
import assert from 'node:assert/strict';
import {validBoundaries,zoneFor,zoneRange} from '../src/kickrcoach/static/zones.mjs';
import {goalFeedback} from '../src/kickrcoach/static/guidance.mjs';

const boundaries=[100,120,140,160];
test('zones have inclusive starts and no gaps at boundaries',()=>{
  for(const [bpm,zone] of [[99,1],[100,2],[119,2],[120,3],[139,3],[140,4],[159,4],[160,5],[200,5]])
    assert.equal(zoneFor(bpm,'LIVE',boundaries),zone);
});
test('no current zone for stale, lost, missing or unconfigured HR',()=>{
  for(const freshness of ['STALE','LOST']) assert.equal(zoneFor(125,freshness,boundaries),null);
  for(const bpm of [null,undefined,NaN,0,-1]) assert.equal(zoneFor(bpm,'LIVE',boundaries),null);
  assert.equal(zoneFor(125,'LIVE',[]),null);
});
test('invalid boundaries are never guessed or sorted automatically',()=>{
  for(const values of [[],[100,120],[100,100,140,160],[120,100,140,160],[100,120,140,'160'],[100,120,140,NaN]])
    assert.equal(validBoundaries(values),false);
  assert.equal(validBoundaries(boundaries),true);
});
test('display ranges agree with the exact classification boundaries',()=>{
  assert.equal(zoneRange(1,boundaries),'Unter 100 bpm');
  assert.equal(zoneRange(2,boundaries),'100–119 bpm');
  assert.equal(zoneRange(3,boundaries),'120–139 bpm');
  assert.equal(zoneRange(4,boundaries),'140–159 bpm');
  assert.equal(zoneRange(5,boundaries),'Ab 160 bpm');
  assert.equal(zoneRange(2,[]),'');
});

import {readFileSync} from 'node:fs';
import vm from 'node:vm';
function dashboard() {
  class Element {
    constructor(){this.textContent='';this.hidden=false;this.disabled=false;this.dataset={};this.children=[];this.options=[];this.attributes={};this.style={setProperty:()=>{}};}
    setAttribute(key,value){this.attributes[key]=value;}
    addEventListener(){}
    replaceChildren(){this.children=[];this.options=[];}
    append(child){this.children.push(child);this.options.push(child);}
    querySelector(){return this;}
  }
  const elements=new Map();
  const get=id=>{if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);};
  get('zone-strip').children=Array.from({length:5},(_,i)=>{const item=new Element();item.dataset.zone=String(i+1);return item;});
  const context=vm.createContext({validBoundaries,zoneFor,zoneRange,goalFeedback,
    document:{getElementById:get,createElement:()=>new Element(),querySelector:()=>get('readonly'),querySelectorAll:()=>[]},
    location:{protocol:'http:',host:'localhost:8000'},performance:{now:()=>100},
    WebSocket:class {},setInterval:()=>{},setTimeout:()=>{},console,fetch:async()=>{throw new Error('Failed to fetch');}});
  const source=readFileSync(new URL('../src/kickrcoach/static/app.js',import.meta.url),'utf8').replace(/^import[^\n]+\n/gm,'');
  vm.runInContext(source+'\nglobalThis.testing={render,clearReadings,command,renderReview};',context);
  return {get,...context.testing};
}
function payload() {
  return {status:'ready',simulated:true,elapsed_sec:30,history:[],hr_zone_boundaries:boundaries,
    trainer:{power_w:40,cadence_rpm:80,connection:'CONNECTED',freshness:'LIVE',age_sec:0},
    hr:{bpm:125,connection:'CONNECTED',freshness:'LIVE',age_sec:0},
    workout:{state:'READY',selected:'baseline',choices:[{id:'baseline',name:'Demo'}],target_w:null,
      planned_target_w:40,block:'Easy',next_block:'Steady',elapsed_sec:0,remaining_sec:240,override_w:null}};
}
test('zone is primary and clears when sensor or server data become stale',()=>{
  const ui=dashboard(), data=payload();
  ui.render(data);
  assert.equal(ui.get('hr-zone').textContent,'Zone 3');
  assert.equal(ui.get('hr').textContent,125);
  assert.equal(ui.get('zone-strip').children[2].attributes['aria-current'],'true');
  data.hr={...data.hr,bpm:null,freshness:'STALE'};ui.render(data);
  assert.equal(ui.get('hr-zone').textContent,'—');
  assert.equal(ui.get('hr').textContent,'—');
  assert.ok(ui.get('zone-strip').children.every(child=>child.attributes['aria-current']==='false'));
  ui.render(payload());ui.clearReadings('Server disconnected');
  assert.equal(ui.get('hr-zone').textContent,'—');
  assert.equal(ui.get('hr').textContent,'—');
  assert.equal(ui.get('start').disabled,true);
});
test('UI gates workout actions by state and hides them for real telemetry',()=>{
  const ui=dashboard(),data=payload();
  ui.render(data);assert.equal(ui.get('start').disabled,false);assert.equal(ui.get('pause').disabled,true);
  data.workout.state='RUNNING';ui.render(data);
  assert.equal(ui.get('pause').disabled,false);assert.equal(ui.get('increase').disabled,false);assert.equal(ui.get('start').disabled,true);
  data.workout.state='PAUSED';ui.render(data);
  assert.equal(ui.get('resume').disabled,false);assert.equal(ui.get('increase').disabled,true);
  data.workout.state='ABORTED';ui.render(data);assert.equal(ui.get('stop').disabled,true);
  data.workout=null;data.simulated=false;ui.render(data);
  assert.equal(ui.get('workout-panel').hidden,true);
});
test('unconfigured zones are explicit and never use default thresholds',()=>{
  const ui=dashboard(),data=payload();data.hr_zone_boundaries=[];ui.render(data);
  assert.equal(ui.get('hr-zone').textContent,'Zonen fehlen');
  assert.equal(ui.get('hr').textContent,125);
});

test('real control is explicitly labelled and recovery disables resume until armed',()=>{
  const ui=dashboard(),data=payload();
  data.simulated=false;data.workout.mode='hardware';data.workout.can_start=false;
  ui.render(data);
  assert.equal(ui.get('start').textContent,'Training am KICKR starten');
  assert.equal(ui.get('start').disabled,true);
  assert.match(ui.get('workout-control-mode').textContent,/ECHTE TRAINERSTEUERUNG/);
  data.workout.state='PAUSED';data.workout.can_resume=false;
  data.workout.notice='STOP wurde nicht bestätigt.';ui.render(data);
  assert.equal(ui.get('resume').disabled,true);
  assert.equal(ui.get('workout-notice').hidden,false);
  assert.match(ui.get('workout-notice').textContent,/nicht bestätigt/);
  data.workout.can_resume=true;ui.render(data);
  assert.equal(ui.get('resume').disabled,false);
});

test('undeliverable STOP gives explicit physical stop guidance',async()=>{
  const ui=dashboard(),data=payload();data.workout.state='RUNNING';data.workout.mode='hardware';
  ui.render(data);
  await ui.command('stop');
  assert.equal(ui.get('workout-error').hidden,false);
  assert.match(ui.get('workout-error').textContent,/STOP konnte nicht bestätigt werden/);
  assert.match(ui.get('workout-error').textContent,/Treten beenden/);
});

test('waiting for pedaling shows an armed start with only STOP available',()=>{
  const ui=dashboard(),data=payload();data.workout.state='WAITING';data.workout.mode='hardware';
  data.workout.maximum_w=300;ui.render(data);
  assert.equal(ui.get('workout-state').textContent,'Warte aufs Losfahren');
  assert.equal(ui.get('clock-label').textContent,'TRAININGSZEIT');
  assert.equal(ui.get('elapsed').textContent,'00:00');
  assert.equal(ui.get('start').disabled,true);
  assert.equal(ui.get('stop').disabled,false);
  assert.equal(ui.get('increase').disabled,true);
  assert.equal(ui.get('pause').disabled,true);
  assert.match(ui.get('planned').textContent,/Limit: 300 W/);
});


test('archive review reports missing values and gates TCX for simulation and active sessions',()=>{
  const ui=dashboard();
  const session={id:'fixture',started_at:'2026-01-01T00:00:00Z',status:'COMPLETED',
    metadata:{workout:{name:'My ride'},simulated:false,rpe:5,cooling:true,notes:'Good'},
    summary:{active_duration_sec:120,average_power_w:100,max_power_w:150,
      average_bpm:null,max_bpm:null,average_cadence_rpm:80,max_cadence_rpm:85,
      coverage_power_w_sec:110,coverage_bpm_sec:0,coverage_cadence_rpm_sec:110,
      sample_count:120,event_count:6,running_sample_count:110}};
  ui.renderReview(session);
  assert.equal(ui.get('review-duration').textContent,'02:00');
  assert.equal(ui.get('review-power').textContent,'100 W / 150 W');
  assert.equal(ui.get('review-hr').textContent,'— / —');
  assert.equal(ui.get('download-tcx').hidden,false);
  assert.equal(ui.get('download-json').href,'/api/sessions/fixture/download/json');
  assert.equal(ui.get('feedback-rpe').value,5);
  session.metadata.simulated=true;ui.renderReview(session);
  assert.equal(ui.get('download-tcx').hidden,true);
  session.metadata.simulated=false;session.status='ACTIVE';ui.renderReview(session);
  assert.equal(ui.get('download-tcx').hidden,true);
  assert.equal(ui.get('session-feedback').hidden,true);
});

test('recording errors stay visible alongside trainer notices',()=>{
  const ui=dashboard(),data=payload();
  data.workout.recording_error='Aufzeichnung fehlgeschlagen';
  ui.render(data);
  assert.equal(ui.get('workout-notice').hidden,false);
  assert.match(ui.get('workout-notice').textContent,/Aufzeichnung/);
});


test('compact device status distinguishes independent connections and unknown server state',()=>{
  const ui=dashboard(),data=payload();
  data.trainer.detail='Verbunden';
  data.hr.connection='CONNECTING';data.hr.detail='Wird gesucht …';
  ui.render(data);
  assert.equal(ui.get('trainer-state').textContent,'Verbunden');
  assert.equal(ui.get('trainer-state').dataset.live,true);
  assert.equal(ui.get('hr-state').textContent,'Nicht verbunden');
  assert.equal(ui.get('hr-state').dataset.live,false);
  assert.equal(ui.get('hr-state').title,'Wird gesucht …');
  data.trainer.connection='DISCONNECTED';data.hr.connection='CONNECTED';
  ui.render(data);
  assert.equal(ui.get('trainer-state').textContent,'Nicht verbunden');
  assert.equal(ui.get('hr-state').textContent,'Verbunden');
  ui.clearReadings('Serverdaten fehlen');
  for(const role of ['trainer','hr']) {
    assert.equal(ui.get(`${role}-state`).textContent,'Unbekannt');
    assert.equal(ui.get(`${role}-state`).dataset.live,'false');
  }
});

test('local workout preview and current instructions are visible without changing control state',()=>{
  const ui=dashboard(),data=payload();
  data.workout.duration_sec=2700;
  data.workout.workout_notes='Observe effort; no automatic RPE decisions.';
  data.workout.block_notes='Keep steady.';
  data.workout.preview=[{name:'Start',duration_sec:60,target_w:50},{name:'Main',duration_sec:300,target_w:130}];
  data.workout.catalog_errors=['invalid.yaml: nicht geladen'];
  ui.render(data);
  assert.equal(ui.get('workout-duration').textContent,'45 min');
  assert.equal(ui.get('workout-blocks').children.length,2);
  assert.equal(ui.get('workout-blocks').children[1].children[2].textContent,'130 W');
  assert.equal(ui.get('block-notes').textContent,'Keep steady.');
  assert.equal(ui.get('catalog-errors').hidden,false);
  assert.equal(ui.get('start').disabled,false);
  data.workout.block_notes=null;data.workout.catalog_errors=[];ui.render(data);
  assert.equal(ui.get('block-notes').hidden,true);
  assert.equal(ui.get('catalog-errors').hidden,true);
});

test('disconnected KICKR permits selection and preview but never start',()=>{
  const ui=dashboard(),data=payload();
  data.workout.state='IDLE';data.workout.mode='hardware';
  data.workout.can_select=true;data.workout.can_start=false;
  data.trainer.connection='DISCONNECTED';
  ui.render(data);
  assert.equal(ui.get('workout-select').disabled,false);
  assert.equal(ui.get('workout-load').disabled,false);
  assert.equal(ui.get('start').disabled,true);
  assert.equal(ui.get('workout-state').textContent,'Warte auf KICKR');
  data.workout.state='WAITING';data.workout.can_select=false;
  ui.render(data);
  assert.equal(ui.get('workout-select').disabled,true);
  ui.clearReadings('Server nicht erreichbar');
  assert.equal(ui.get('workout-select').disabled,true);
});

test('goal boundaries are inclusive and stale or missing data never score as within target',()=>{
  for(const [value,status] of [[79,'below'],[80,'within'],[85,'within'],[90,'within'],[91,'above']])
    assert.equal(goalFeedback(value,80,90,true,true),status);
  for(const value of [null,undefined,NaN]) assert.equal(goalFeedback(value,80,90,true,true),'unavailable');
  assert.equal(goalFeedback(85,80,90,true,false),'unavailable');
  assert.equal(goalFeedback(85,80,90,false,true),'inactive');
  assert.equal(goalFeedback(85,undefined,undefined,true,true),'unconfigured');
});

test('cadence coaching uses the current block while power deviations leave ERG in charge',()=>{
  const ui=dashboard(),data=payload();
  data.workout.state='RUNNING';data.workout.target_w=70;data.workout.power_tolerance_w=5;
  data.workout.cadence_target={minimum_rpm:80,maximum_rpm:90};
  data.trainer.power_w=69;data.trainer.cadence_rpm=85;
  ui.render(data);
  assert.equal(ui.get('power-card').dataset.feedback,'within');
  assert.equal(ui.get('cadence-status').textContent,'✓ Im Ziel');
  assert.equal(ui.get('cadence-goal').textContent,'Ziel 80–90 rpm');
  data.trainer.cadence_rpm=91;data.trainer.power_w=80;ui.render(data);
  assert.equal(ui.get('cadence-hint').textContent,'Langsamer treten');
  assert.equal(ui.get('power-card').dataset.feedback,'above');
  assert.match(ui.get('power-hint').textContent,/ERG regelt/);
  data.trainer.cadence_rpm=79;data.trainer.power_w=60;ui.render(data);
  assert.equal(ui.get('cadence-hint').textContent,'Schneller treten');
  assert.equal(ui.get('power-card').dataset.feedback,'below');
  // A subsequent intentional lower-cadence block is classified by its own range.
  data.workout.cadence_target={minimum_rpm:60,maximum_rpm:70};data.trainer.cadence_rpm=65;ui.render(data);
  assert.equal(ui.get('cadence-card').dataset.feedback,'within');
  data.workout.override_w=5;data.workout.target_w=75;data.trainer.power_w=75;ui.render(data);
  assert.match(ui.get('power-goal').textContent,/75 W/);
  assert.equal(ui.get('power-card').dataset.feedback,'within');
  data.trainer.freshness='STALE';ui.render(data);
  assert.equal(ui.get('cadence-card').dataset.feedback,'unavailable');
  assert.equal(ui.get('power-card').dataset.feedback,'unavailable');
  assert.equal(ui.get('cadence-hint').textContent,'');
  data.trainer.freshness='LIVE';
  ui.clearReadings('Serverdaten fehlen');
  assert.equal(ui.get('cadence-card').dataset.feedback,'unavailable');
  assert.equal(ui.get('cadence-hint').textContent,'');
  data.workout.cadence_target=null;ui.render(data);
  assert.equal(ui.get('cadence-status').textContent,'Keine Vorgabe');
  data.workout.state='PAUSED';data.workout.cadence_target={minimum_rpm:80,maximum_rpm:90};ui.render(data);
  assert.equal(ui.get('cadence-status').textContent,'Pausiert');
  assert.equal(ui.get('cadence-hint').textContent,'');
});
