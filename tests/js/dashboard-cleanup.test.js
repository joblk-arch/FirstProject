'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const root = path.resolve(__dirname, '../..');
const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const collapse = fs.readFileSync(path.join(root, 'static/dashboard-collapsible.js'), 'utf8');
const harness = fs.readFileSync(path.join(__dirname, 'renderers.test.js'), 'utf8');
const start = harness.indexOf('function matchBrace(');
const end = harness.indexOf('// --- Extract the three renderers');
assert(start >= 0 && end > start);
const extract = vm.runInNewContext(harness.slice(start, end) + '\nextractFunction;');
const summaryPanels = {
  'recent-workflows-panel': 'recent-workflows-hint',
  'recent-agent-activity-panel': 'recent-agent-activity-hint',
  'work-distribution-panel': 'work-distribution-hint',
  'token-ledger-panel': 'token-ledger-hint',
};
const ids = ['alerts-panel', 'user-management', ...Object.keys(summaryPanels)];
function context(role = 'viewer') {
  const elements = {};
  for (const id of [...ids, 'alerts', 'alerts-history', 'alerts-hint', 'jobs',
    'metrics', 'projects', 'models', 'workflows', 'updated', 'error', ...Object.values(summaryPanels)]) {
    elements[id] = { id, tagName: ids.includes(id) ? 'DETAILS' : 'DIV',
      open: false, hidden: id === 'user-management', innerHTML: '', textContent: '',
      listeners: {}, addEventListener(type, fn) {
        (this.listeners[type] ||= []).push(fn);
      } };
  }
  let writes = 0;
  const store = {};
  const ctx = { console, document: { readyState: 'complete',
    getElementById: id => elements[id] || null },
    window: { localStorage: { getItem: key => store[key] ?? null,
      setItem: (key, value) => { writes++; store[key] = value; } } } };
  vm.createContext(ctx);
  vm.runInContext(collapse, ctx);
  for (const name of ['renderRecentWorkflowReliability', 'renderWorkflowBottlenecks',
    'renderCompletionRecovery', 'renderRunningNow', 'renderAgentActivity', 'renderValidationDiagnostics', 'loadUpsStatus']) {
    ctx[name] = () => {};
  }
  ctx._handle401 = () => false;
  vm.runInContext(app.slice(0, app.indexOf('const dialog =')) +
    `\nconst sessionIdentity = {role: ${JSON.stringify(role)}};\n` +
    ['repairBadge', 'renderSummaries', 'render', 'renderAlerts'].map(name => {
      const source = extract(app, name); assert(source, name); return source;
    }).join('\n') + '\nasync ' + extract(app, 'loadAlerts') + '\nasync ' + extract(app, 'load'), ctx);
  return { ctx, elements, store, writes: () => writes };
}
async function main() {
  // All four real production wrappers participate in persistence/render checks.
  {
    const {ctx, elements: els, store, writes} = context();
    const key = 'firstproject.dashboard.expansion.v1';
    const wrappers = Object.keys(summaryPanels).map(id => els[id]);
    for (const panel of wrappers) {
      assert.strictEqual(panel.listeners.toggle.length, 1);
      panel.open = false;
      panel.listeners.toggle.forEach(fn => fn());
    }
    const listeners = wrappers.map(panel => panel.listeners.toggle.slice());
    const hints = Object.values(summaryPanels).map(id => els[id]);
    const saved = store[key];
    const before = writes();
    for (const n of [1, 2, 3]) {
      const old = hints.map(el => el.textContent);
      ctx.render({projects: Array.from({length:n}, (_, i) => ({name:`p${i}`})),
        recent_workflows: Array.from({length:n}, () => ({status:'running'})),
        counts:[{status:'running',count:n},{status:'queued',count:n+1}],
        usage:[{total_tokens:n*100}], generated_at:'2026-10-16'});
      assert.deepStrictEqual(hints.map(el => el.textContent), [
        `Recent sample: ${n} running · 0 need attention`, `${n} running · ${n+1} queued`,
        `${n} projects`, `${n*100} recorded tokens`]);
      hints.forEach((el, i) => { assert.notStrictEqual(el.textContent, old[i]);
        assert.strictEqual(els[Object.values(summaryPanels)[i]], el); });
      wrappers.forEach((panel,i) => {
        assert.strictEqual(els[panel.id], panel);
        assert.strictEqual(panel.open, false);
        assert.deepStrictEqual(panel.listeners.toggle, listeners[i]);
      });
      assert.strictEqual(store[key], saved);
      assert.strictEqual(writes(), before);
    }
    for (const panel of wrappers) {
      const prior = writes(); panel.open = true;
      panel.listeners.toggle.forEach(fn => fn());
      assert.strictEqual(writes(), prior+1);
      assert.strictEqual(JSON.parse(store[key])[panel.id], true);
    }
  }
  // Preserve malformed-data and numeric-boundary regression coverage.
  {
    const {ctx, elements: els} = context();
    for (const data of [null, {}, {recent_workflows:null,counts:{},projects:false,usage:'bad'}]) {
      ctx.renderSummaries(data);
      Object.values(summaryPanels).forEach(id => assert.strictEqual(els[id].textContent, 'Unavailable'));
    }
    const cases = [
      ['recent_workflows','recent-workflows-hint',[null, [], true, {status:1}]],
      ['counts','recent-agent-activity-hint',[null, [], true, {status:1,count:0},
        ...[true,-1,1.2,'2',NaN,Infinity,Number.MAX_SAFE_INTEGER+1].map(count=>({status:'running',count}))]],
      ['projects','work-distribution-hint',[null, [], true, {name:1}]],
      ['usage','token-ledger-hint',[null, [], true,
        ...[true,-1,1.2,'2',NaN,Infinity,Number.MAX_SAFE_INTEGER+1].map(total_tokens=>({total_tokens}))]],
    ];
    for (const [field,id,entries] of cases) for (const entry of entries) {
      ctx.renderSummaries({[field]:[entry]});
      assert.strictEqual(els[id].textContent,'Unavailable', `${field}: ${JSON.stringify(entry)}`);
    }
    ctx.renderSummaries({recent_workflows:[],counts:[],projects:[],usage:[]});
    assert.deepStrictEqual(Object.values(summaryPanels).map(id=>els[id].textContent),[
      'Recent sample: 0 running · 0 need attention','0 running · 0 queued','0 projects','0 recorded tokens']);
    ctx.renderSummaries({counts:[{status:'running',count:Number.MAX_SAFE_INTEGER},{status:'running',count:1}],
      usage:[{total_tokens:Number.MAX_SAFE_INTEGER},{total_tokens:1}]});
    assert.strictEqual(els['recent-agent-activity-hint'].textContent,'Unavailable');
    assert.strictEqual(els['token-ledger-hint'].textContent,'Unavailable');
    ctx.renderSummaries({recent_workflows:[{status:'failed'},{status:'blocked'},{status:'completed'}],
      counts:[{status:'running',count:7},{status:'queued',count:3},{status:'completed',count:99}],
      jobs:[{status:'running'}], projects:[{name:'<script>x</script>'}], usage:[{total_tokens:1234}]});
    assert.strictEqual(els['recent-workflows-hint'].textContent,'Recent sample: 0 running · 2 need attention');
    assert.strictEqual(els['recent-agent-activity-hint'].textContent,'7 running · 3 queued');
    assert.strictEqual(els['work-distribution-hint'].textContent,'1 projects');
    assert.strictEqual(els['work-distribution-hint'].innerHTML,'');
    assert.strictEqual(els['token-ledger-hint'].textContent,'1,234 recorded tokens');
  }
  for (const mode of ['network','500','json','401']) {
    const {ctx,elements:els} = context();
    Object.values(summaryPanels).forEach(id=>els[id].textContent='stale');
    ctx._handle401 = response => response.status===401;
    ctx.fetch = async () => {
      if (mode==='network') throw new Error('offline');
      return {status:Number(mode)||200,ok:mode==='json',json:async()=>{throw new Error('bad JSON');}};
    };
    await ctx.load();
    Object.values(summaryPanels).forEach(id=>assert.strictEqual(els[id].textContent,'Unavailable'));
  }
  for (const role of ['viewer', 'operator', 'admin']) {
    const { ctx, elements: els, store, writes } = context(role);
    const identities = ids.map(id => els[id]);
    const listeners = ids.map(id => els[id].listeners.toggle.slice());
    for (const count of [2, 0, 1]) {
      const before = writes();
      const saved = JSON.stringify(store);
      const active = Array.from({length: count}, (_, i) => ({job_id: `job${i}`, status: 'failed'}));
      ctx.renderAlerts({active, history: [{job_id: 'old', acknowledged_at: '2026-10-16'}]});
      assert.strictEqual(els['alerts-hint'].textContent,
        count ? `${count} awaiting acknowledgement` : 'None active');
      assert.strictEqual(els.alerts.innerHTML.includes('data-ack-job-id='), role === 'admin' && count > 0);
      if (role === 'admin' && count) {
        assert(els.alerts.innerHTML.includes('class="acknowledge-btn"'));
        assert(!els.alerts.innerHTML.includes('<input'), 'acknowledgement stays one-click');
      }
      ctx.render({projects: [], recent_workflows: [{id: 'abc123def4', project: 'firstproject',
        objective: `Refresh ${count}`, status: 'completed'}], generated_at: '2026-10-16'});
      assert(els.workflows.innerHTML.includes(`Refresh ${count}`));
      assert(els.workflows.innerHTML.includes('data-workflow-id="abc123def4"'));
      ids.forEach((id, i) => {
        assert.strictEqual(els[id], identities[i]);
        assert.strictEqual(els[id].open, false);
        assert.deepStrictEqual(els[id].listeners.toggle, listeners[i]);
      });
      assert.strictEqual(els['user-management'].hidden, true, 'render/persistence never reveals admin root');
      assert.strictEqual(writes(), before);
      assert.strictEqual(JSON.stringify(store), saved);
    }
    for (const id of ids) {
      const before = writes();
      els[id].open = true;
      els[id].listeners.toggle.forEach(fn => fn());
      assert.strictEqual(writes(), before + 1);
      assert.strictEqual(JSON.parse(store['firstproject.dashboard.expansion.v1'])[id], true);
    }
  }
  for (const mode of ['401', '500', 'network', 'json']) {
    const {ctx, elements: els} = context();
    els['alerts-hint'].textContent = '9 awaiting acknowledgement';
    let authCalls = 0;
    ctx._handle401 = response => {
      authCalls++;
      assert.strictEqual(response.status, 401);
      assert.strictEqual(els['alerts-hint'].textContent, 'Alert data unavailable',
        'clear hint before session handler');
      return true;
    };
    ctx.fetch = async () => {
      if (mode === 'network') throw new Error('offline');
      return { status: Number(mode) || 200, ok: mode === 'json',
        json: async () => { throw new Error('invalid JSON'); } };
    };
    await ctx.loadAlerts();
    assert.strictEqual(els['alerts-hint'].textContent, 'Alert data unavailable');
    assert.strictEqual(authCalls, mode === '401' ? 1 : 0);
  }
  const {ctx, elements: els} = context();
  ctx.fetch = async () => ({status: 200, ok: true,
    json: async () => ({active: [{job_id:'new'}], history: []})});
  await ctx.loadAlerts();
  assert.strictEqual(els['alerts-hint'].textContent, '1 awaiting acknowledgement');
  console.log('All dashboard cleanup behavior checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
