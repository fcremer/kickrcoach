import test from 'node:test';
import assert from 'node:assert/strict';
import {validBoundaries,zoneFor,zoneRange} from '../src/kickrcoach/static/zones.mjs';

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
  const context=vm.createContext({validBoundaries,zoneFor,zoneRange,
    document:{getElementById:get,createElement:()=>new Element(),querySelector:()=>get('readonly'),querySelectorAll:()=>[]},
    location:{protocol:'http:',host:'localhost:8000'},performance:{now:()=>100},
    WebSocket:class {},setInterval:()=>{},setTimeout:()=>{},console,fetch:async()=>{throw new Error('Failed to fetch');}});
  const source=readFileSync(new URL('../src/kickrcoach/static/app.js',import.meta.url),'utf8').replace(/^import[^\n]+\n/,'');
  vm.runInContext(source+'\nglobalThis.testing={render,clearReadings,command};',context);
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
