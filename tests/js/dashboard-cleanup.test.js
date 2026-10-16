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
const ids = ['alerts-panel', 'recent-workflows-panel', 'user-management'];
function context(role = 'viewer') {
  const elements = {};
  for (const id of [...ids, 'alerts', 'alerts-history', 'alerts-hint', 'jobs',
    'metrics', 'projects', 'models', 'workflows', 'updated', 'error']) {
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
    'renderCompletionRecovery', 'renderRunningNow', 'renderAgentActivity', 'renderValidationDiagnostics']) {
    ctx[name] = () => {};
  }
  ctx._handle401 = () => false;
  vm.runInContext(app.slice(0, app.indexOf('const dialog =')) +
    `\nconst sessionIdentity = {role: ${JSON.stringify(role)}};\n` +
    ['repairBadge', 'render', 'renderAlerts'].map(name => {
      const source = extract(app, name); assert(source, name); return source;
    }).join('\n') + '\nasync ' + extract(app, 'loadAlerts'), ctx);
  return { ctx, elements, store, writes: () => writes };
}
async function main() {
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
