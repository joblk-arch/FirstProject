'use strict';

// Focused behavior tests for static/dashboard-collapsible.js
//
// Tests storage restoration, defaults, invalid JSON, throwing storage,
// native details defaults, and persistence across renderer updates.
// Uses Node's vm module with minimal DOM/localStorage stubs.
//
// Exit codes:
//   0  all checks passed
//   2  could not read static/dashboard-collapsible.js
//   1  an assertion failed

const path = require('path');
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const projectRoot = path.resolve(__dirname, '..', '..');
const jsPath = process.argv[2] || path.join(projectRoot, 'static', 'dashboard-collapsible.js');

let source;
try {
  source = fs.readFileSync(jsPath, 'utf8');
} catch (err) {
  console.error(`FATAL: cannot read ${jsPath}: ${err.message}`);
  process.exit(2);
}

// --- Stub infrastructure ---

function makeDetailsEl(id, open) {
  return {
    id,
    open: !!open,
    tagName: 'DETAILS',
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    removeEventListener(ev, fn) {
      if (this.listeners[ev]) this.listeners[ev] = this.listeners[ev].filter(f => f !== fn);
    },
    dispatchToggle() {
      (this.listeners['toggle'] || []).forEach(fn => fn.call(this, { target: this }));
    },
  };
}

function makeLocalStorageStub(initialValue, opts = {}) {
  let store = {};
  if (initialValue !== undefined && initialValue !== null) {
    store = { 'firstproject.dashboard.expansion.v1': initialValue };
  }
  return {
    _store: store,
    _throwOnAccess: !!opts.throwOnAccess,
    _throwOnSet: !!opts.throwOnSet,
    getItem(key) {
      if (this._throwOnAccess) throw new Error('SecurityError: localStorage access denied');
      return Object.prototype.hasOwnProperty.call(this._store, key) ? this._store[key] : null;
    },
    setItem(key, val) {
      if (this._throwOnSet) throw new Error('QuotaExceededError');
      this._store[key] = String(val);
    },
    removeItem(key) {
      if (this._throwOnAccess) throw new Error('SecurityError');
      delete this._store[key];
    },
    get length() { return Object.keys(this._store).length; },
    key(i) { return Object.keys(this._store)[i] || null; },
  };
}

function makeDocumentStub(elements) {
  const map = new Map(Object.entries(elements));
  return {
    readyState: 'complete',
    getElementById(id) {
      if (map.has(id)) return map.get(id);
      return null;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
    body: { appendChild() {} },
  };
}

// --- Check runner ---
let passed = 0;
let total = 0;
function check(label, fn) {
  total++;
  try {
    fn();
    passed++;
    console.log(`ok - ${label}`);
  } catch (err) {
    console.error(`NOT OK - ${label}: ${err.message}`);
    process.exit(1);
  }
}

// --- Helper: build a vm context ---
function buildContext({ storageValue, throwOnAccess, throwOnSet, elements }) {
  const ls = makeLocalStorageStub(storageValue, { throwOnAccess, throwOnSet });
  const doc = makeDocumentStub(elements || {});
  const win = {
    addEventListener() {},
    removeEventListener() {},
    localStorage: ls,
  };
  const ctx = {
    document: doc,
    window: win,
    localStorage: ls,
    console,
    JSON, Object, Array, String, Number, Boolean, Math, Error, RegExp,
    setTimeout, clearTimeout,
  };
  vm.createContext(ctx);
  return { ctx, ls, doc };
}

// Standard element set matching the HTML
function standardElements() {
  return {
    'reliability-section': makeDetailsEl('reliability-section', false),
    'cluster-health-panel': makeDetailsEl('cluster-health-panel', false),
    'ups-panel': makeDetailsEl('ups-panel', false),
    'work-distribution-panel': makeDetailsEl('work-distribution-panel', false),
    'token-ledger-panel': makeDetailsEl('token-ledger-panel', false),
    'start-build-panel': makeDetailsEl('start-build-panel', true),
    'recent-agent-activity-panel': makeDetailsEl('recent-agent-activity-panel', true),
    'alerts-panel': makeDetailsEl('alerts-panel', false),
    'recent-workflows-panel': makeDetailsEl('recent-workflows-panel', false),
    'user-management': makeDetailsEl('user-management', false),
  };
}

for (const id of ['alerts-panel', 'recent-workflows-panel', 'user-management']) {
  check(`${id}: own toggle persists and reload restores both booleans`, () => {
    for (const opened of [true, false]) {
      const els = standardElements();
      els[id].hidden = id === 'user-management';
      const {ctx, ls} = buildContext({elements: els});
      vm.runInContext(source, ctx);
      assert.strictEqual(els[id].listeners.toggle.length, 1);
      els[id].open = opened;
      els[id].dispatchToggle();
      const saved = ls.getItem('firstproject.dashboard.expansion.v1');
      assert.strictEqual(JSON.parse(saved)[id], opened);
      const fresh = standardElements();
      const reload = buildContext({elements: fresh, storageValue: saved});
      vm.runInContext(source, reload.ctx);
      assert.strictEqual(fresh[id].open, opened);
      assert.strictEqual(els[id].hidden, id === 'user-management');
    }
  });
  check(`${id}: old, invalid and unavailable storage preserve closed default`, () => {
    for (const options of [{storageValue: '{bad'}, {storageValue: '[]'},
      {storageValue: '{"start-build-panel":false}'}, {throwOnAccess: true},
      {storageValue: JSON.stringify({[id]: 'open'})}]) {
      const els = standardElements();
      const {ctx} = buildContext({...options, elements: els});
      vm.runInContext(source, ctx);
      assert.strictEqual(els[id].open, false);
      assert.strictEqual(els['recent-agent-activity-panel'].open, true);
    }
  });
}

// --- Test 1: Defaults applied when storage is empty ---
check('defaults: all sections use their intended open/closed state when no storage exists', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: null, elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, false, 'reliability-section should be collapsed by default');
  assert.strictEqual(doc.getElementById('cluster-health-panel').open, false, 'cluster-health-panel should be collapsed by default');
  assert.strictEqual(doc.getElementById('ups-panel').open, false, 'ups-panel should be collapsed by default');
  assert.strictEqual(doc.getElementById('work-distribution-panel').open, false, 'work-distribution-panel should be collapsed by default');
  assert.strictEqual(doc.getElementById('token-ledger-panel').open, false, 'token-ledger-panel should be collapsed by default');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'start-build-panel should be open by default');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, true, 'recent-agent-activity-panel should be open by default');
});

// --- Test 2: Storage restoration ---
check('storage: saved open states are restored on init', () => {
  const saved = JSON.stringify({
    'reliability-section': true,
    'cluster-health-panel': true,
    'ups-panel': false,
    'work-distribution-panel': true,
    'token-ledger-panel': false,
    'start-build-panel': false,
    'recent-agent-activity-panel': false,
  });
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: saved, elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, true, 'reliability-section should be restored open');
  assert.strictEqual(doc.getElementById('cluster-health-panel').open, true, 'cluster-health-panel should be restored open');
  assert.strictEqual(doc.getElementById('ups-panel').open, false, 'ups-panel should be restored closed');
  assert.strictEqual(doc.getElementById('work-distribution-panel').open, true, 'work-distribution-panel should be restored open');
  assert.strictEqual(doc.getElementById('token-ledger-panel').open, false, 'token-ledger-panel should be restored closed');
  assert.strictEqual(doc.getElementById('start-build-panel').open, false, 'start-build-panel should be restored closed');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, false, 'recent-agent-activity-panel should be restored closed');
});

// --- Test 3: Invalid JSON in storage falls back to defaults ---
check('storage: invalid JSON falls back to defaults without throwing', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: '{not valid json!!', elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, false, 'should use default (closed)');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'should use default (open)');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, true, 'should use default (open)');
});

// --- Test 4: Throwing localStorage (Safari private mode) ---
check('storage: throwing localStorage is caught and defaults are used', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ throwOnAccess: true, elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, false, 'should use default (closed)');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'should use default (open)');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, true, 'should use default (open)');
});

// --- Test 5: Partial/missing individual values use defaults ---
check('storage: missing individual keys use their defaults', () => {
  const saved = JSON.stringify({
    'reliability-section': true,
    // others missing
  });
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: saved, elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, true, 'saved value should be used');
  assert.strictEqual(doc.getElementById('cluster-health-panel').open, false, 'missing key should use default (closed)');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'missing key should use default (open)');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, true, 'missing key should use default (open)');
});

// --- Test 6: Non-boolean values in storage use defaults ---
check('storage: non-boolean values (strings, numbers, null) fall back to defaults', () => {
  const saved = JSON.stringify({
    'reliability-section': 'yes',
    'cluster-health-panel': 1,
    'start-build-panel': null,
    'recent-agent-activity-panel': 'open',
  });
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: saved, elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, false, 'string "yes" should fall back to default (closed)');
  assert.strictEqual(doc.getElementById('cluster-health-panel').open, false, 'number 1 should fall back to default (closed)');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'null should fall back to default (open)');
  assert.strictEqual(doc.getElementById('recent-agent-activity-panel').open, true, 'string "open" should fall back to default (open)');
});

check('recent activity: its own toggle persists false and reload restores it', () => {
  const els = standardElements();
  const { ctx, ls } = buildContext({ elements: els });
  vm.runInContext(source, ctx);
  const panel = els['recent-agent-activity-panel'];
  assert.strictEqual(panel.listeners.toggle.length, 1);
  panel.open = false;
  panel.dispatchToggle();
  const saved = ls.getItem('firstproject.dashboard.expansion.v1');
  assert.strictEqual(JSON.parse(saved)['recent-agent-activity-panel'], false);
  const fresh = standardElements();
  assert.strictEqual(fresh['recent-agent-activity-panel'].open, true);
  const reload = buildContext({ elements: fresh, storageValue: saved });
  vm.runInContext(source, reload.ctx);
  assert.strictEqual(fresh['recent-agent-activity-panel'].open, false);
});

check('recent activity: actual render preserves panel state, listener and storage', () => {
  const els = standardElements();
  const panel = els['recent-agent-activity-panel'];
  for (const id of ['jobs', 'metrics', 'projects', 'models', 'workflows', 'updated', 'error']) {
    els[id] = { id, innerHTML: '', textContent: '', hidden: false };
  }
  els.jobs.parentElement = panel;
  const { ctx, ls, doc } = buildContext({ elements: els });
  vm.runInContext(source, ctx);
  const listeners = panel.listeners.toggle.slice();
  let writes = 0;
  const setItem = ls.setItem.bind(ls);
  ls.setItem = (...args) => { writes++; setItem(...args); };
  const harness = fs.readFileSync(path.join(__dirname, 'renderers.test.js'), 'utf8');
  const start = harness.indexOf('function matchBrace(');
  const end = harness.indexOf('// --- Extract the three renderers');
  assert(start >= 0 && end > start);
  const extract = vm.runInNewContext(harness.slice(start, end) + '\nextractFunction;');
  const app = fs.readFileSync(path.join(projectRoot, 'static', 'app.js'), 'utf8');
  const render = extract(app, 'render');
  assert(render, 'actual dashboard render must exist');
  // Execute production formatting helpers and the entire render function.
  // Unrelated child cards are stubbed; jobs rendering itself is not replaced.
  for (const name of ['renderRecentWorkflowReliability', 'renderWorkflowBottlenecks',
    'renderCompletionRecovery', 'renderRunningNow', 'renderAgentActivity', 'renderValidationDiagnostics']) {
    ctx[name] = () => {};
  }
  vm.runInContext(app.slice(0, app.indexOf('const dialog =')) + '\n' + render, ctx);
  for (const opened of [false, true]) {
    panel.open = opened;
    panel.dispatchToggle();
    const beforeWrites = writes;
    const saved = ls.getItem('firstproject.dashboard.expansion.v1');
    for (const status of ['running', 'completed']) {
      ctx.render({ projects: [], jobs: [{ role: 'tester', project: 'firstproject', status,
        workflow_id: 'abc123def4', total_tokens: 123, duration_seconds: 2 }],
        generated_at: '2026-10-16T00:00:00Z' });
      assert(els.jobs.innerHTML.includes(`status ${status}`), 'actual jobs output must change');
      assert(els.jobs.innerHTML.includes('data-workflow-id="abc123def4"'));
      assert.strictEqual(doc.getElementById('recent-agent-activity-panel'), panel);
      assert.strictEqual(els.jobs.parentElement, panel);
      assert.strictEqual(panel.open, opened);
      assert.deepStrictEqual(panel.listeners.toggle, listeners);
      assert.strictEqual(writes, beforeWrites, 'render must not persist preferences');
      assert.strictEqual(ls.getItem('firstproject.dashboard.expansion.v1'), saved);
    }
  }
  const before = writes;
  panel.open = false;
  panel.dispatchToggle();
  assert.strictEqual(writes, before + 1, 'one listener remains after renders');
  assert.strictEqual(JSON.parse(ls.getItem('firstproject.dashboard.expansion.v1'))[panel.id], false);
});

// --- Test 7: Toggle event saves state to storage ---
check('toggle: user toggling a section persists the new state', () => {
  const els = standardElements();
  const { ctx, doc, ls } = buildContext({ storageValue: null, elements: els });
  vm.runInContext(source, ctx);

  // Simulate user opening the reliability section
  const rs = doc.getElementById('reliability-section');
  rs.open = true;
  rs.dispatchToggle();

  const stored = ls.getItem('firstproject.dashboard.expansion.v1');
  assert(stored !== null, 'storage should have been written after toggle');
  const parsed = JSON.parse(stored);
  assert.strictEqual(parsed['reliability-section'], true, 'stored value should be true after opening');
});

// --- Test 8: Page reload restores expansion ---
check('persistence: re-initializing with saved state restores user-changed expansion', () => {
  const els = standardElements();
  const { ctx, ls } = buildContext({ storageValue: null, elements: els });
  vm.runInContext(source, ctx);

  // User opens reliability section
  const rs = els['reliability-section'];
  rs.open = true;
  rs.dispatchToggle();

  // Simulate a page reload: new elements start at HTML defaults, but storage has the saved state
  const els2 = standardElements();
  const savedState = ls._store['firstproject.dashboard.expansion.v1'];
  const { ctx: ctx2, doc: doc2 } = buildContext({ storageValue: savedState, elements: els2 });
  vm.runInContext(source, ctx2);

  assert.strictEqual(doc2.getElementById('reliability-section').open, true,
    'reliability-section should be restored open from storage after re-init');
});

// --- Test 9: No private data in storage ---
check('storage: only boolean expansion states are stored, no user/job data', () => {
  const els = standardElements();
  const { ctx, ls } = buildContext({ storageValue: null, elements: els });
  vm.runInContext(source, ctx);

  const rs = els['reliability-section'];
  rs.open = true;
  rs.dispatchToggle();

  const stored = ls.getItem('firstproject.dashboard.expansion.v1');
  assert(stored !== null, 'storage should have data');
  const parsed = JSON.parse(stored);
  for (const [k, v] of Object.entries(parsed)) {
    assert.strictEqual(typeof v, 'boolean',
      `stored value for "${k}" should be boolean, got ${typeof v}: ${JSON.stringify(v)}`);
  }
});

// --- Test 10: Versioned storage key ---
check('storage: uses a versioned key (contains a version indicator)', () => {
  const els = standardElements();
  const { ctx, ls } = buildContext({ storageValue: null, elements: els });
  vm.runInContext(source, ctx);

  const rs = els['reliability-section'];
  rs.open = true;
  rs.dispatchToggle();

  const keys = Object.keys(ls._store);
  assert(keys.length > 0, 'at least one key should be stored');
  const hasVersion = keys.some(k => /v\d+|version/i.test(k));
  assert(hasVersion, `storage key should be versioned, got: ${keys.join(', ')}`);
});

// --- Test 11: Throwing setItem (quota exceeded) does not crash ---
check('storage: throwing setItem is caught gracefully', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: null, throwOnSet: true, elements: els });
  vm.runInContext(source, ctx);

  // Toggle should not throw
  const rs = doc.getElementById('reliability-section');
  rs.open = true;
  rs.dispatchToggle(); // should not throw

  // Element state should still be correct (user's toggle is respected in DOM)
  assert.strictEqual(rs.open, true, 'element open state should remain true even if storage write fails');
});

// --- Test 12: JSON array in storage is treated as invalid ---
check('storage: JSON array in storage is treated as invalid, defaults used', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ storageValue: '[true, false]', elements: els });
  vm.runInContext(source, ctx);

  assert.strictEqual(doc.getElementById('reliability-section').open, false, 'array should be treated as invalid, default (closed) used');
  assert.strictEqual(doc.getElementById('start-build-panel').open, true, 'array should be treated as invalid, default (open) used');
});

check('actual renderer refresh preserves wrapper state and listeners without storage writes', () => {
  const els = standardElements();
  els['completion-recovery'] = { id: 'completion-recovery', innerHTML: '' };
  const { ctx, ls } = buildContext({ elements: els });
  vm.runInContext(source, ctx);
  const wrapper = els['reliability-section'];
  wrapper.open = true;
  wrapper.dispatchToggle();
  const saved = JSON.stringify(ls._store);
  const listeners = wrapper.listeners.toggle.slice();
  let writes = 0;
  const setItem = ls.setItem.bind(ls);
  ls.setItem = (...args) => { writes++; setItem(...args); };

  // Reuse the existing bounded, template/comment-aware function extractor.
  const harness = fs.readFileSync(path.join(__dirname, 'renderers.test.js'), 'utf8');
  const start = harness.indexOf('function matchBrace(');
  const end = harness.indexOf('// --- Extract the three renderers');
  assert(start >= 0 && end > start, 'existing extractor must be present');
  const extractor = vm.runInNewContext(harness.slice(start, end) + '\nextractFunction;');
  const app = fs.readFileSync(path.join(projectRoot, 'static', 'app.js'), 'utf8');
  const renderer = extractor(app, 'renderCompletionRecovery');
  assert(renderer, 'actual renderer must be found');
  ctx.$ = id => ctx.document.getElementById(id);
  ctx.safe = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;');
  vm.runInContext(renderer, ctx);
  const empty = { sample_count: 0, implementer_jobs: 0, observed_jobs: 0,
    unknown_jobs: 0, attempted_jobs: 0, recovered_jobs: 0, failed_jobs: 0,
    not_attempted_jobs: 0 };
  for (const data of [null, empty, { ...empty, sample_count: 1 }]) {
    const before = els['completion-recovery'].innerHTML;
    ctx.renderCompletionRecovery(data);
    assert.notStrictEqual(els['completion-recovery'].innerHTML, before);
    assert.strictEqual(ctx.document.getElementById('reliability-section'), wrapper);
    assert.strictEqual(wrapper.open, true);
    assert.deepStrictEqual(wrapper.listeners.toggle, listeners);
    assert.strictEqual(JSON.stringify(ls._store), saved);
    assert.strictEqual(writes, 0, 'polling must not persist preferences');
  }
  wrapper.open = false;
  wrapper.dispatchToggle();
  assert.strictEqual(writes, 1, 'toggle listener remains functional after refreshes');
});

check('loading document initializes only after DOMContentLoaded', () => {
  const els = standardElements();
  const { ctx, doc } = buildContext({ elements: els,
    storageValue: JSON.stringify({ 'reliability-section': true }) });
  doc.readyState = 'loading';
  let ready;
  doc.addEventListener = (event, fn) => {
    assert.strictEqual(event, 'DOMContentLoaded');
    ready = fn;
  };
  vm.runInContext(source, ctx);
  assert.strictEqual(els['reliability-section'].open, false);
  assert.strictEqual(els['reliability-section'].listeners.toggle, undefined);
  assert.strictEqual(typeof ready, 'function');
  ready();
  assert.strictEqual(els['reliability-section'].open, true);
  assert.strictEqual(els['reliability-section'].listeners.toggle.length, 1);
});

// --- Summary ---
console.log(`\n${passed}/${total} checks passed`);
if (passed < total) process.exit(1);
