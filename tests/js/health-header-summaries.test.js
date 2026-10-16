'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const root = path.resolve(__dirname, '../..');
const app = fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const users = fs.readFileSync(path.join(root,'static/user-management.js'),'utf8');
const collapse = fs.readFileSync(path.join(root,'static/dashboard-collapsible.js'),'utf8');
const harness = fs.readFileSync(path.join(__dirname,'renderers.test.js'),'utf8');
const extract = vm.runInNewContext(harness.slice(harness.indexOf('function matchBrace('),
  harness.indexOf('// --- Extract the three renderers'))+'\nextractFunction;');
const panels = ['cluster-health-panel','ups-panel','reliability-section','user-management'];
function setup() {
  const els = {};
  for (const id of [...panels,'system-health-hint','ups-summary-hint','reliability-summary-hint',
    'accounts-summary-hint','cluster-health','health-timestamp','ups-status','ups-timestamp',
    'recent-workflow-reliability']) {
    els[id] = {id, tagName:panels.includes(id)?'DETAILS':'DIV',open:false,
      hidden:id==='user-management',innerHTML:'',textContent:'Unavailable',listeners:{},
      querySelector:()=>null, addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}};
  }
  let writes=0, requests=0, response={ok:true,status:200,json:async()=>({users:[]})};
  const store={};
  const ctx={console,document:{readyState:'complete',getElementById:id=>els[id]||null,
    addEventListener(){},querySelector:()=>null}, window:{},
    fetch:async()=>{requests++;if(response instanceof Error)throw response;return response;}};
  ctx.window=ctx;
  ctx.localStorage={getItem:key=>store[key]??null,setItem:(key,value)=>{writes++;store[key]=value;}};
  vm.createContext(ctx);
  vm.runInContext(collapse,ctx);
  ctx.renderStatusCountsCard=()=>{};
  ctx._handle401=()=>true;
  vm.runInContext(app.slice(0,app.indexOf('const dialog ='))+'\n'+
    ['renderClusterHealth','renderClusterHealthError','renderUpsStatus','renderRecentWorkflowReliability']
      .map(name=>{const s=extract(app,name);assert(s,name);return s;}).join('\n')+'\n'+
    ['loadClusterHealth','loadUpsStatus'].map(name=>'async '+extract(app,name)).join('\n'),ctx);
  vm.runInContext(users,ctx);
  return {ctx,els,store,writes:()=>writes,requests:()=>requests,setResponse:r=>response=r};
}
const freshUps={source_health:'fresh',source_fresh:true,stale:false,monitor_state:'running',
  ups_present:true,power_source:'ac',battery_percent:90};
const goodHealth={overall:'healthy',services:[{name:'gateway',status:'healthy'}],lm_studio:{status:'healthy'}};
const rel={sample_count:2,successful:1,failed:1,active:0,excluded:0};
const flush=async()=>{for(let i=0;i<10;i++)await Promise.resolve();};
async function main(){
  const {ctx,els,store,writes}=setup();
  const wrappers=panels.map(id=>els[id]);
  wrappers.forEach(el=>{el.open=false;el.listeners.toggle.forEach(fn=>fn());});
  const saved=JSON.stringify(store), before=writes(), listeners=wrappers.map(el=>el.listeners.toggle.slice());
  for(const [data,expected] of [[goodHealth,'Healthy'],[{overall:'healthy'},'Unavailable'],
    [{...goodHealth,services:[]},'Unavailable'],[{...goodHealth,services:[{status:'unknown'}]},'Unavailable'],
    [{...goodHealth,services:[{status:'nonsense'}]},'Unavailable'],[{...goodHealth,services:[null]},'Unavailable'],
    [{...goodHealth,lm_studio:{}},'Unavailable'],[{...goodHealth,lm_studio:{status:'offline'}},'Needs attention'],
    [{...goodHealth,overall:'degraded'},'Needs attention'],[{...goodHealth,overall:'unknown'},'Unavailable']]){
    // Existing body assumes valid service objects; its fetch wrapper catches malformed payloads.
    try{ctx.renderClusterHealth(data);}catch(error){ctx.renderClusterHealthError();}
    assert.strictEqual(els['system-health-hint'].textContent,expected);
  }
  for(const [data,expected] of [[freshUps,'Utility power · Battery 90%'],
    [{...freshUps,power_source:'ups',battery_percent:40},'On battery · Battery 40%'],
    [{...freshUps,source_health:'stale',source_fresh:false,stale:true},'Stale'],
    [{...freshUps,source_health:'stale'},'Degraded (contradictory flags)'],
    [{...freshUps,monitor_state:'stopped'},'Degraded (monitor stopped)'],
    [{...freshUps,ups_present:false},'No UPS detected'],
    [{...freshUps,battery_percent:true},'Degraded (invalid battery)'],
    [{...freshUps,battery_percent:20.5},'Degraded (invalid battery)'],[null,'Unavailable']]){
    ctx.renderUpsStatus(data); assert.strictEqual(els['ups-summary-hint'].textContent,expected);
    assert(els['ups-status'].innerHTML.includes('Shutdown below 40% on UPS power only'));
  }
  for(const [data,expected] of [[rel,'Recent sample: 1 failed · 0 active'],
    [{sample_count:0,successful:0,failed:0,active:0,excluded:0},'No recent workflows'],
    [{...rel,failed:true},'Unavailable'],[{...rel,sample_count:21},'Unavailable'],
    [{...rel,sample_count:3},'Unavailable'],[null,'Unavailable']]){
    ctx.renderRecentWorkflowReliability(data);assert.strictEqual(els['reliability-summary-hint'].textContent,expected);
  }
  wrappers.forEach((el,i)=>{assert.strictEqual(els[el.id],el);assert.strictEqual(el.open,false);
    assert.deepStrictEqual(el.listeners.toggle,listeners[i]);});
  assert.strictEqual(JSON.stringify(store),saved);assert.strictEqual(writes(),before);
  assert.strictEqual(els['user-management'].hidden,true);
  for(const method of ['loadClusterHealth','loadUpsStatus'])for(const mode of ['401','500','network','json']){
    const s=setup();const hint=method==='loadClusterHealth'?'system-health-hint':'ups-summary-hint';
    s.els[hint].textContent='stale healthy';
    s.ctx._handle401=()=>{assert.strictEqual(s.els[hint].textContent,'Unavailable');return true;};
    s.setResponse(mode==='network'?new Error('offline'):{status:Number(mode)||200,ok:mode==='json',
      json:async()=>{throw new Error('bad JSON');}});
    await s.ctx[method]();assert.strictEqual(s.els[hint].textContent,'Unavailable');
  }
  for(const role of ['viewer','operator']){
    const s=setup();s.ctx.initUserManagement({role,username:'test'});await flush();
    assert.strictEqual(s.requests(),0);assert.strictEqual(s.els['user-management'].hidden,true);
    assert.strictEqual(s.els['accounts-summary-hint'].textContent,'Unavailable');
  }
  for(const [body,expected] of [[{users:[{disabled:false},{disabled:true}]},'Admin only · 2 accounts · 1 disabled'],
    [{users:[]},'Admin only · 0 accounts · 0 disabled'],[{},'Admin only · Unavailable'],
    [{users:[{disabled:'false'}]},'Admin only · Unavailable'],[null,'Admin only · Unavailable']]){
    const s=setup();s.setResponse({status:200,ok:true,json:async()=>body});
    const panel=s.els['user-management'], listener=panel.listeners.toggle.slice();
    const priorStore=JSON.stringify(s.store), priorWrites=s.writes();
    s.ctx.initUserManagement({role:'admin',username:'test'});await flush();
    assert.strictEqual(s.requests(),1);assert.strictEqual(s.els['user-management'].hidden,false);
    assert.strictEqual(s.els['accounts-summary-hint'].textContent,expected);
    assert.strictEqual(s.els['user-management'].open,false);
    assert.strictEqual(s.els['user-management'],panel);
    assert.deepStrictEqual(panel.listeners.toggle,listener);
    assert.strictEqual(JSON.stringify(s.store),priorStore);assert.strictEqual(s.writes(),priorWrites);
  }
  for(const mode of ['401','500','network','json']){
    const s=setup();s.els['accounts-summary-hint'].textContent='Admin only · stale';
    s.ctx._handle401=()=>{assert.strictEqual(s.els['accounts-summary-hint'].textContent,'Admin only · Unavailable');return true;};
    s.setResponse(mode==='network'?new Error('offline'):{status:Number(mode)||200,ok:mode==='json',
      json:async()=>{throw new Error('bad JSON');}});
    s.ctx.initUserManagement({role:'admin',username:'test'});await flush();
    assert.strictEqual(s.els['accounts-summary-hint'].textContent,'Admin only · Unavailable');
  }
  console.log('Health/UPS/reliability/account header checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
