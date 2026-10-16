'use strict';

// Regression harness for the cluster-health renderers in static/app.js.
//
// It loads the ACTUAL top-level functions renderClusterHealth,
// renderStatusCountsCard and renderClusterHealthError via bounded extraction
// (no whole-app evaluation, no network, no DOM). It then runs a small set of
// assertions using Node's built-in fs / vm / assert modules.
//
// Exit codes:
//   0  all checks passed
//   2  could not read static/app.js
//   3  a required top-level function could not be extracted
//   4  a required function was not defined after evaluation
//   1  an assertion failed

const path = require('path');
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

// --- Resolve static/app.js (override with argv[2] if needed) ---
const projectRoot = path.resolve(__dirname, '..', '..');
const appJsPath = process.argv[2] || path.join(projectRoot, 'static', 'app.js');

let source;
try {
  source = fs.readFileSync(appJsPath, 'utf8');
} catch (err) {
  console.error(`FATAL: cannot read ${appJsPath}: ${err.message}`);
  process.exit(2);
}

// --- Bounded extraction of top-level named functions ---
// matchBrace finds the index of the closing brace that matches the opening
// brace at openIdx. It is string/template/comment aware so that braces inside
// string literals, template literals (including nested ${...}) and comments do
// not confuse the balance.
function matchBrace(src, openIdx) {
  const stack = [{ mode: 'code', entryDepth: 0 }];
  let depth = 0;
  let i = openIdx;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    const c2 = i + 1 < n ? src[i + 1] : '';
    const top = stack[stack.length - 1];
    if (top.mode === 'code') {
      if (c === '/' && c2 === '/') { // line comment
        i += 2;
        while (i < n && src[i] !== '\n') i++;
        continue;
      }
      if (c === '/' && c2 === '*') { // block comment
        i += 2;
        while (i < n && !(src[i] === '*' && src[i + 1] === '/')) i++;
        i += 2;
        continue;
      }
      if (c === "'" || c === '"') { // single/double quoted string
        const q = c;
        i++;
        while (i < n) {
          if (src[i] === '\\') { i += 2; continue; }
          if (src[i] === q) { i++; break; }
          i++;
        }
        continue;
      }
      if (c === '`') { // template literal
        stack.push({ mode: 'template', entryDepth: depth });
        i++;
        continue;
      }
      if (c === '{') { depth++; i++; continue; }
      if (c === '}') {
        depth--;
        if (depth === top.entryDepth) {
          stack.pop();
          if (stack.length === 0) return i;
        }
        i++;
        continue;
      }
      i++;
      continue;
    }
    // template mode
    if (c === '\\') { i += 2; continue; }
    if (c === '`') { stack.pop(); i++; continue; }
    if (c === '$' && c2 === '{') { // template interpolation
      depth++;
      stack.push({ mode: 'code', entryDepth: depth - 1 });
      i += 2;
      continue;
    }
    i++;
    continue;
  }
  return -1;
}

// Extract the full source of a top-level `function name(...) { ... }`.
function extractFunction(src, name) {
  const re = new RegExp('\\bfunction\\s+' + name + '\\s*\\(', 'g');
  const m = re.exec(src);
  if (!m) return null;
  const openIdx = src.indexOf('{', m.index);
  if (openIdx === -1) return null;
  const closeIdx = matchBrace(src, openIdx);
  if (closeIdx === -1) return null;
  return src.slice(m.index, closeIdx + 1);
}

// --- Extract the three renderers; fail clearly if any is missing ---
const REQUIRED = ['renderClusterHealth', 'renderStatusCountsCard', 'renderClusterHealthError', 'renderRunningNow', 'renderAgentActivity', 'renderUpsStatus', 'renderRecentWorkflowReliability', 'renderWorkflowBottlenecks', 'renderCompletionRecovery', 'renderValidationDiagnostics'];
const extracted = {};
for (const name of REQUIRED) {
  const fn = extractFunction(source, name);
  if (fn === null) {
    console.error(`FATAL: could not extract top-level function "${name}" from ${appJsPath}`);
    process.exit(3);
  }
  extracted[name] = fn;
}

// --- Minimal DOM stubs ---
function makeDocumentStub() {
  const elements = new Map();
  return {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, { id, innerHTML: '', textContent: '', hidden: false });
      }
      return elements.get(id);
    },
  };
}

// Faithful helper (not a renderer) used by renderClusterHealth for escaping.
const safe = (s) => String(s ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

const documentStub = makeDocumentStub();
const context = {
  document: documentStub,
  $: (id) => documentStub.getElementById(id),
  safe,
  duration: (n) => n == null ? '\u2014' : n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${n % 60}s`,
  // built-ins the renderers rely on
  Number, String, Array, Date, Intl, Object, Math, JSON, Boolean, RegExp, Error,
  console,
};
vm.createContext(context);

// Evaluate ONLY the extracted renderers (not the whole app) so that no
// whole-app initialization or network execution happens.
vm.runInContext(Object.values(extracted).join('\n;\n'), context);

for (const name of REQUIRED) {
  if (typeof context[name] !== 'function') {
    console.error(`FATAL: "${name}" is not defined after evaluation`);
    process.exit(4);
  }
}

const { renderClusterHealth, renderStatusCountsCard, renderClusterHealthError, renderRunningNow, renderAgentActivity, renderUpsStatus, renderCompletionRecovery } = context;
const $ = (id) => documentStub.getElementById(id);

// --- Tiny check runner ---
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

const countUnavailable = (html) => (html.match(/Unavailable/g) || []).length;

// A valid ISO timestamp used in tests that need a non-Unavailable timestamp.
const VALID_TS = '2024-06-15T12:00:00Z';

// --- Test 1: agent_queue.status_counts wiring via renderClusterHealth ---
check('renderClusterHealth wires agent_queue.status_counts into the reliability card', () => {
  renderClusterHealth({
    overall: 'healthy',
    generated_at: VALID_TS,
    services: [],
    lm_studio: {},
    agent_queue: { running: 1, queued: 2, status_counts: { completed: 10, failed: 3, blocked: 1 } },
  });
  const card = $('reliability-card').innerHTML;
  assert(card.includes('<strong>10</strong>'), `expected completed=10 in: ${card}`);
  assert(card.includes('<strong>3</strong>'), `expected failed=3 in: ${card}`);
  assert(card.includes('<strong>1</strong>'), `expected blocked=1 in: ${card}`);
  assert(!card.includes('Unavailable'), `unexpected Unavailable in: ${card}`);
});

// --- Test 2: nonzero and zero counts ---
check('nonzero counts render as their numeric values', () => {
  renderStatusCountsCard({ completed: 5, failed: 2, blocked: 7 }, VALID_TS);
  const card = $('reliability-card').innerHTML;
  assert(card.includes('<strong>5</strong>'), card);
  assert(card.includes('<strong>2</strong>'), card);
  assert(card.includes('<strong>7</strong>'), card);
  assert(!card.includes('Unavailable'), card);
});

check('zero counts render as 0 (not Unavailable)', () => {
  renderStatusCountsCard({ completed: 0, failed: 0, blocked: 0 }, VALID_TS);
  const card = $('reliability-card').innerHTML;
  assert.strictEqual((card.match(/<strong>0<\/strong>/g) || []).length, 3, card);
  assert(!card.includes('Unavailable'), card);
});

// --- Test 3: null / bool / negative / oversized / malicious -> Unavailable, no injected markup ---
check('null counts show Unavailable', () => {
  renderStatusCountsCard({ completed: null, failed: null, blocked: null }, VALID_TS);
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('boolean counts show Unavailable', () => {
  renderStatusCountsCard({ completed: true, failed: false, blocked: true }, VALID_TS);
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('negative counts show Unavailable', () => {
  renderStatusCountsCard({ completed: -1, failed: -5, blocked: -100 }, VALID_TS);
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('oversized counts (> 10,000,000) show Unavailable', () => {
  renderStatusCountsCard({ completed: 10000001, failed: 1e9, blocked: 100000000 }, VALID_TS);
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('count == _SAFE_BOUND (10,000,000) renders as a value, not Unavailable', () => {
  renderStatusCountsCard({ completed: 10000000, failed: 10000000, blocked: 10000000 }, VALID_TS);
  const card = $('reliability-card').innerHTML;
  assert.strictEqual(countUnavailable(card), 0, card);
  assert.strictEqual((card.match(/<strong>10000000<\/strong>/g) || []).length, 3, card);
});

check('malicious strings show Unavailable without injected markup', () => {
  renderStatusCountsCard({
    completed: '<script>alert("xss")</script>',
    failed: '<img src=x onerror=alert(1)>',
    blocked: '"><svg onload=alert(2)>',
  }, VALID_TS);
  const card = $('reliability-card').innerHTML;
  assert.strictEqual(countUnavailable(card), 3, card);
  assert(!card.includes('<script>'), `injected <script> in: ${card}`);
  assert(!card.includes('<img'), `injected <img> in: ${card}`);
  assert(!card.includes('<svg'), `injected <svg> in: ${card}`);
  assert(!card.includes('onerror'), `injected onerror in: ${card}`);
  assert(!card.includes('onload'), `injected onload in: ${card}`);
});

// --- Test 4: renderClusterHealthError after success resets old counts ---
check('renderClusterHealthError after a successful render resets counts to Unavailable', () => {
  renderClusterHealth({
    overall: 'healthy',
    generated_at: VALID_TS,
    services: [],
    lm_studio: {},
    agent_queue: { running: 1, queued: 0, status_counts: { completed: 42, failed: 7, blocked: 2 } },
  });
  let card = $('reliability-card').innerHTML;
  assert(card.includes('<strong>42</strong>'), `expected 42 before error: ${card}`);
  assert(!card.includes('Unavailable'), `unexpected Unavailable before error: ${card}`);

  renderClusterHealthError();
  card = $('reliability-card').innerHTML;
  assert.strictEqual(countUnavailable(card), 4, `expected 4 Unavailable (3 counts + 1 timestamp) after error: ${card}`);
  assert(!card.includes('<strong>42</strong>'), `stale count 42 survived error: ${card}`);
  assert(!card.includes('<strong>7</strong>'), `stale count 7 survived error: ${card}`);
  assert(!card.includes('<strong>2</strong>'), `stale count 2 survived error: ${card}`);
});

// --- Test 5: timestamp display ---
check('valid generated_at displays a human-readable Last checked time', () => {
  renderStatusCountsCard({ completed: 1, failed: 0, blocked: 0 }, VALID_TS);
  const card = $('reliability-card').innerHTML;
  assert(card.includes('Last checked:'), `missing Last checked label: ${card}`);
  assert(!card.includes('Last checked: Unavailable'), `should not be Unavailable for valid date: ${card}`);
  // The timestamp should contain some non-empty content after the label
  const m = card.match(/Last checked: (.+?)<\/span>/);
  assert(m, `no timestamp content found: ${card}`);
  assert(m[1].length > 0, `timestamp content is empty: ${card}`);
});

check('missing generated_at (null) displays Last checked: Unavailable', () => {
  renderStatusCountsCard({ completed: 1, failed: 0, blocked: 0 }, null);
  const card = $('reliability-card').innerHTML;
  assert(card.includes('Last checked: Unavailable'), `expected Unavailable: ${card}`);
});

check('missing generated_at (undefined) displays Last checked: Unavailable', () => {
  renderStatusCountsCard({ completed: 1, failed: 0, blocked: 0 });
  const card = $('reliability-card').innerHTML;
  assert(card.includes('Last checked: Unavailable'), `expected Unavailable: ${card}`);
});

check('invalid generated_at displays Last checked: Unavailable', () => {
  renderStatusCountsCard({ completed: 1, failed: 0, blocked: 0 }, 'not-a-valid-date');
  const card = $('reliability-card').innerHTML;
  assert(card.includes('Last checked: Unavailable'), `expected Unavailable for invalid date: ${card}`);
});

check('malicious generated_at does not inject raw HTML', () => {
  renderStatusCountsCard({ completed: 1, failed: 0, blocked: 0 }, '<script>alert("xss")</script>');
  const card = $('reliability-card').innerHTML;
  // new Date('<script>...') is invalid, so should show Unavailable
  assert(card.includes('Last checked: Unavailable'), `expected Unavailable for malicious input: ${card}`);
  assert(!card.includes('<script>'), `injected script tag: ${card}`);
});

check('success-to-error resets timestamp to Unavailable', () => {
  renderClusterHealth({
    overall: 'healthy',
    generated_at: VALID_TS,
    services: [],
    lm_studio: {},
    agent_queue: { running: 1, queued: 0, status_counts: { completed: 5, failed: 1, blocked: 0 } },
  });
  let card = $('reliability-card').innerHTML;
  assert(!card.includes('Last checked: Unavailable'), `valid timestamp should not show Unavailable before error: ${card}`);

  renderClusterHealthError();
  card = $('reliability-card').innerHTML;
  assert(card.includes('Last checked: Unavailable'), `timestamp should reset to Unavailable after error: ${card}`);
});

// --- renderRunningNow ---

check('renderRunningNow: empty state shows "No jobs running"', () => {
  renderRunningNow([]);
  const html = $('running-now').innerHTML;
  assert(html.includes('No jobs running'), `expected "No jobs running", got: ${html}`);
});

check('renderRunningNow: filters only running jobs', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build', duration_seconds: 30 },
    { id: '2', status: 'completed', project: 'beta', stage: 'test', duration_seconds: 60 },
    { id: '3', status: 'queued', project: 'gamma', stage: 'deploy', duration_seconds: null },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('alpha'), `should show running job project: ${html}`);
  assert(!html.includes('beta'), `should not show completed job: ${html}`);
  assert(!html.includes('gamma'), `should not show queued job: ${html}`);
});

check('renderRunningNow: multiple running jobs all shown', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build', duration_seconds: 10 },
    { id: '2', status: 'running', project: 'beta', stage: 'test', duration_seconds: 120 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('alpha'), `should show first job: ${html}`);
  assert(html.includes('beta'), `should show second job: ${html}`);
});

check('renderRunningNow: missing duration_seconds shows "Timing unavailable"', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build', duration_seconds: null },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('Timing unavailable'), `expected "Timing unavailable", got: ${html}`);
  assert(!html.includes('\u2014'), `should not show em-dash for missing timing: ${html}`);
});

check('renderRunningNow: stage falls back to role when stage is missing', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: null, role: 'implementer', duration_seconds: 5 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('implementer'), `should show role fallback: ${html}`);
});

check('renderRunningNow: malicious project/stage strings are escaped', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: '<script>alert(1)</script>', stage: '<img src=x onerror=alert(2)>', duration_seconds: 5 },
  ]);
  const html = $('running-now').innerHTML;
  assert(!html.includes('<script>'), `raw <script> in output: ${html}`);
  assert(!html.includes('<img'), `raw <img in output: ${html}`);
  assert(!html.includes('</script>'), `raw </script> in output: ${html}`);
});

check('renderRunningNow: shows duration with "reported elapsed time" label', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build', duration_seconds: 90 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('1m 30s'), `should show formatted duration: ${html}`);
  assert(html.includes('reported elapsed time'), `should label as reported elapsed time: ${html}`);
});

check('renderRunningNow: duration_seconds undefined shows "Timing unavailable"', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build' },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('Timing unavailable'), `expected "Timing unavailable" for undefined duration, got: ${html}`);
});

check('renderRunningNow: duration_seconds 0 shows "0s reported elapsed time"', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: 'build', duration_seconds: 0 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('0s'), `should show 0s for zero duration: ${html}`);
  assert(html.includes('reported elapsed time'), `should label as reported elapsed time: ${html}`);
  assert(!html.includes('Timing unavailable'), `zero is a valid duration, not unavailable: ${html}`);
});

check('renderRunningNow: both stage and role missing shows em-dash', () => {
  renderRunningNow([
    { id: '1', status: 'running', project: 'alpha', stage: null, role: null, duration_seconds: 5 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('\u2014'), `should show em-dash when both stage and role are null: ${html}`);
});

check('renderRunningNow: missing project shows em-dash', () => {
  renderRunningNow([
    { id: '1', status: 'running', stage: 'build', duration_seconds: 5 },
  ]);
  const html = $('running-now').innerHTML;
  assert(html.includes('\u2014'), `should show em-dash for missing project: ${html}`);
});

// ─── UPS Status Renderer Tests ───────────────────────────────────────────────

check('UPS: fresh AC power shows healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('healthy'), `should show healthy: ${html}`);
  assert(html.includes('AC Power'), `should show AC Power: ${html}`);
  assert(html.includes('100%'), `should show 100%: ${html}`);
  assert(html.includes('Yes'), `should show UPS present Yes: ${html}`);
  assert(html.includes('running'), `should show running: ${html}`);
});

check('UPS: battery 0 displays 0 not dash', () => {
  const data = {
    power_source: 'ups', battery_percent: 0, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('0%'), `should show 0% not dash: ${html}`);
  assert(!html.includes('\u2014</span>'), `should not show dash for battery: ${html}`);
});

check('UPS: stale data shows Stale not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 80, ups_present: true,
    monitor_state: 'running', source_fresh: false, stale: true,
    source_health: 'stale', last_updated: '2024-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('Stale'), `should show Stale: ${html}`);
  assert(!html.includes('healthy'), `should not show healthy: ${html}`);
});

check('UPS: missing/null data shows Unavailable', () => {
  renderUpsStatus(null);
  const html = $('ups-status').innerHTML;
  assert(html.includes('Unavailable'), `should show Unavailable: ${html}`);
  assert(!html.includes('healthy'), `should not show healthy: ${html}`);
});

check('UPS: fresh data + stopped monitor shows degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'stopped', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('ups-badge degraded'), `badge should be degraded: ${html}`);
  assert(html.includes('stopped'), `should show stopped: ${html}`);
  assert(!html.includes('ups-badge healthy'), `badge should not be healthy: ${html}`);
});

check('UPS: fresh absent UPS shows degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: null, ups_present: false,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('ups-badge degraded'), `badge should be degraded: ${html}`);
  assert(html.includes('No UPS detected'), `should show No UPS detected: ${html}`);
  assert(!html.includes('ups-badge healthy'), `badge should not be healthy: ${html}`);
});

check('UPS: fetch failure (null) resets to Unavailable', () => {
  // Simulate previously healthy then fetch failure
  const goodData = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(goodData);
  assert($('ups-status').innerHTML.includes('healthy'), 'should be healthy first');
  // Now fetch fails
  renderUpsStatus(null);
  const html = $('ups-status').innerHTML;
  assert(html.includes('Unavailable'), `should reset to Unavailable: ${html}`);
  assert(!html.includes('healthy'), `should not keep old green: ${html}`);
});

check('UPS: malicious HTML in last_event is escaped', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z',
    last_event: '<script>alert("xss")</script><img onerror=alert(1) src=x>'
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(!html.includes('<script>'), `should not contain raw script tag: ${html}`);
  assert(!html.includes('<img'), `should not contain raw img tag: ${html}`);
  assert(html.includes('&lt;script&gt;'), `should contain escaped script: ${html}`);
});

check('UPS: malicious HTML in power_source is escaped', () => {
  const data = {
    power_source: '<b onmouseover=alert(1)>bold</b>', battery_percent: 50,
    ups_present: true, monitor_state: 'running', source_fresh: true,
    stale: false, source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  // Invalid power_source should fall back to 'unknown'
  assert(html.includes('Unknown'), `should show Unknown for invalid power_source: ${html}`);
  assert(!html.includes('<b '), `should not contain raw b tag: ${html}`);
});

check('UPS: fixed policy text is exact and cannot be changed by API', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z',
    last_event: '', policy: 'SHUTDOWN AT 0% IMMEDIATELY'
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('Shutdown below 40% on UPS power only'), `should show exact policy: ${html}`);
  assert(!html.includes('SHUTDOWN AT 0%'), `should not show API policy override: ${html}`);
  assert(!html.includes('<=40'), `should not contain <=40 wording: ${html}`);
  assert(!html.includes('timer'), `should not contain timer wording: ${html}`);
});

check('UPS: invalid battery values fall back safely', () => {
  // Negative
  renderUpsStatus({ power_source: 'ac', battery_percent: -5, ups_present: true, monitor_state: 'running', source_fresh: true, stale: false, source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: '' });
  assert($('ups-status').innerHTML.includes('\u2014'), `negative battery should show dash`);
  // Over 100
  renderUpsStatus({ power_source: 'ac', battery_percent: 150, ups_present: true, monitor_state: 'running', source_fresh: true, stale: false, source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: '' });
  assert($('ups-status').innerHTML.includes('\u2014'), `over-100 battery should show dash`);
  // String instead of number
  renderUpsStatus({ power_source: 'ac', battery_percent: '50', ups_present: true, monitor_state: 'running', source_fresh: true, stale: false, source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: '' });
  assert($('ups-status').innerHTML.includes('\u2014'), `string battery should show dash`);
  // Boolean (not a valid number type in JS for our check)
  renderUpsStatus({ power_source: 'ac', battery_percent: true, ups_present: true, monitor_state: 'running', source_fresh: true, stale: false, source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: '' });
  assert($('ups-status').innerHTML.includes('\u2014'), `boolean battery should show dash`);
});

check('UPS: invalid date shows dash', () => {
  renderUpsStatus({ power_source: 'ac', battery_percent: 50, ups_present: true, monitor_state: 'running', source_fresh: true, stale: false, source_health: 'fresh', last_updated: 'not-a-date', last_event: '' });
  const html = $('ups-status').innerHTML;
  assert(html.includes('\u2014'), `invalid date should show dash: ${html}`);
});

check('UPS: no shutdown button or mutation controls', () => {
  const data = {
    power_source: 'ups', battery_percent: 30, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(!html.includes('<button'), `should not contain button: ${html}`);
  assert(!html.includes('<input'), `should not contain input: ${html}`);
  assert(!html.includes('<form'), `should not contain form: ${html}`);
  assert(!html.includes('onclick'), `should not contain onclick: ${html}`);
});

check('UPS: read-only controls (no interactive elements)', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(!html.includes('<a '), `should not contain links: ${html}`);
  assert(!html.includes('<select'), `should not contain select: ${html}`);
  assert(!html.includes('<textarea'), `should not contain textarea: ${html}`);
});

check('UPS: timestamp hint updates', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  assert($('ups-timestamp').textContent.includes('Updated'), `timestamp should show Updated: ${$('ups-timestamp').textContent}`);
  // Null data
  renderUpsStatus(null);
  assert($('ups-timestamp').textContent === 'No data', `timestamp should show No data: ${$('ups-timestamp').textContent}`);
});

check('UPS: last_event bounded to 500 chars', () => {
  const longEvent = 'Z'.repeat(1000);
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: longEvent
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  // Count Z's - only appears in last_event (no other field uses Z)
  const zCount = (html.match(/Z/g) || []).length;
  assert(zCount <= 500, `last_event should be bounded to 500 chars, got ${zCount} Z's`);
  assert(zCount === 500, `last_event should be exactly 500 chars, got ${zCount}`);
});

// --- Targeted UPS badge correction tests ---

check('UPS: fresh AC + unknown monitor => degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'unknown', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for unknown monitor: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy with unknown monitor: ${html}`);
});

check('UPS: fresh AC + missing presence => degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 100,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for missing presence: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy with missing presence: ${html}`);
});

check('UPS: fresh AC + invalid battery (boolean) => degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: true, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for boolean battery: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy with boolean battery: ${html}`);
});

check('UPS: fresh AC + invalid battery (fraction) => degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 50.5, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for fraction battery: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy with fraction battery: ${html}`);
});

check('UPS: missing source_health + stale true => unavailable', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: false, stale: true,
    last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('unavailable'), `should show unavailable for missing source_health: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy: ${html}`);
  assert(!html.includes('stale'), `must not show stale when source_health missing: ${html}`);
});

check('UPS: malformed source_health (number) + stale true => unavailable', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: false, stale: true,
    source_health: 42, last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('unavailable'), `should show unavailable for malformed source_health: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy: ${html}`);
});

check('UPS: contradictory source_fresh true + stale true => degraded not healthy', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: true,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for contradictory flags: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy with contradictory flags: ${html}`);
});

check('UPS: fresh valid 0% battery on AC => healthy shows 0%', () => {
  const data = {
    power_source: 'ac', battery_percent: 0, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('healthy'), `should show healthy for valid 0%: ${html}`);
  assert(html.includes('0%'), `should show 0%: ${html}`);
});

check('UPS: fresh valid 0% battery on UPS => warning shows 0%', () => {
  const data = {
    power_source: 'ups', battery_percent: 0, ups_present: true,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('warning'), `should show warning for 0% on UPS: ${html}`);
  assert(html.includes('0%'), `should show 0%: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy on UPS power: ${html}`);
});

check('UPS: stopped monitor + fresh source => degraded visible', () => {
  const data = {
    power_source: 'ac', battery_percent: 80, ups_present: true,
    monitor_state: 'stopped', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for stopped: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy when stopped: ${html}`);
});

check('UPS: absent UPS + fresh source => degraded visible', () => {
  const data = {
    power_source: 'ac', battery_percent: 100, ups_present: false,
    monitor_state: 'running', source_fresh: true, stale: false,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('degraded'), `should show degraded for absent: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy when absent: ${html}`);
});

check('UPS: actual source_health stale => stale badge', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: false, stale: false,
    source_health: 'stale', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('stale'), `should show stale badge: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy: ${html}`);
});

check('UPS: explicit stale true from valid source => stale badge', () => {
  const data = {
    power_source: 'ac', battery_percent: 50, ups_present: true,
    monitor_state: 'running', source_fresh: false, stale: true,
    source_health: 'fresh', last_updated: '2025-01-01T00:00:00Z', last_event: ''
  };
  renderUpsStatus(data);
  const html = $('ups-status').innerHTML;
  assert(html.includes('stale'), `should show stale for explicit stale: ${html}`);
  assert(!html.includes('healthy'), `must not be healthy: ${html}`);
});

const recentFixture = () => ({
  sample_count: 4, successful: 2, failed: 1, active: 1, excluded: 0,
  rate: 1, failure_stage_counts: {plan: 1}, blocked_stage_counts: {review: 1},
  duration: {sample_count: 3, median_seconds: 60, average_seconds: 70},
  tokens: {total: 100, measured_jobs: 2, missing_jobs: 1, incomplete: true},
});
const recentHtml = (data) => {
  context.renderRecentWorkflowReliability(data);
  return $('recent-workflow-reliability').innerHTML;
};
check('Recent reliability: exact schema and derived denominator', () => {
  const html = recentHtml(recentFixture());
  for (const expected of ['Latest 4 workflows (up to 20)', '66.7%', '3 assessed outcomes',
    '60.0s (3 samples)', 'Recorded tokens: 100', '2 measured / 1 missing jobs', 'Incomplete',
    'Failed stages: plan: 1', 'Blocked stages: review: 1', 'Approved / merged / pushed']) {
    assert(html.includes(expected), `${expected} absent: ${html}`);
  }
});
check('Recent reliability: recorded zero stays zero', () => {
  const data = recentFixture();
  data.tokens = {total: 0, measured_jobs: 2, missing_jobs: 0, incomplete: false};
  const html = recentHtml(data);
  assert(html.includes('Recorded tokens: 0 (2 measured / 0 missing jobs)'), html);
  assert(!html.includes('Incomplete'), html);
});
check('Recent reliability: unknown tokens and duration not invented', () => {
  const data = recentFixture();
  data.tokens = {total: null, measured_jobs: 0, missing_jobs: 3};
  data.duration = {sample_count: 0, median_seconds: null};
  const html = recentHtml(data);
  assert(html.includes('Median completion: Not recorded'), html);
  assert(html.includes('Recorded tokens: Not recorded (0 measured / 3 missing jobs)'), html);
});
check('Recent reliability: active-only never claims 100 percent', () => {
  const data = recentFixture();
  Object.assign(data, {successful: 0, failed: 0, active: 4});
  const html = recentHtml(data);
  assert(html.includes('Success rate: Unavailable (0 assessed outcomes)'), html);
  assert(!html.includes('100.0%'), html);
});
check('Recent reliability: valid empty window and invalid counts', () => {
  assert(recentHtml({sample_count: 0, successful: 0, failed: 0, active: 0, excluded: 0}).includes('No recent workflows'));
  for (const sample of [true, 1.5, 21, NaN, -1]) {
    assert(recentHtml({...recentFixture(), sample_count: sample}).includes('Unavailable'));
  }
  assert(recentHtml({...recentFixture(), successful: true}).includes('Unavailable'));
  assert(recentHtml({...recentFixture(), failed: 4}).includes('Unavailable'));
});
check('Recent reliability: missing data resets earlier numbers', () => {
  recentHtml(recentFixture());
  const html = recentHtml(null);
  assert(html.includes('Unavailable') && !html.includes('66.7%'), html);
});
check('Recent reliability: malicious fields and stage keys never rendered', () => {
  const data = recentFixture();
  data.failure_stage_counts['<img src=x onerror=alert(1)>'] = 1;
  data.workflows = [{id: '<script>bad</script>', status: 'healthy'}];
  const html = recentHtml(data);
  assert(!html.includes('<img') && !html.includes('<script>') && !html.includes('onerror'), html);
  assert(html.includes('Failed stages: plan: 1'), html);
});

// --- Wiring: render calls renderRecentWorkflowReliability ---
check('render calls renderRecentWorkflowReliability(data.recent_workflow_reliability)', () => {
  const renderSrc = extractFunction(source, 'render');
  assert(renderSrc !== null, 'render function must exist');
  assert(renderSrc.includes('renderRecentWorkflowReliability(data.recent_workflow_reliability)'),
    'render must call renderRecentWorkflowReliability(data.recent_workflow_reliability)');
});

// --- Wiring: load catch calls renderRecentWorkflowReliability(null) before Session expired return ---
check('load catch calls renderRecentWorkflowReliability(null) before Session expired return', () => {
  const loadSrc = extractFunction(source, 'load');
  assert(loadSrc !== null, 'load function must exist');
  const catchIdx = loadSrc.indexOf('catch');
  assert(catchIdx !== -1, 'load must have a catch block');
  const catchSrc = loadSrc.slice(catchIdx);
  const nullCallIdx = catchSrc.indexOf('renderRecentWorkflowReliability(null)');
  assert(nullCallIdx !== -1, 'load catch must call renderRecentWorkflowReliability(null)');
  const sessionIdx = catchSrc.indexOf("Session expired");
  assert(sessionIdx !== -1, 'load catch must reference Session expired');
  assert(nullCallIdx < sessionIdx, 'renderRecentWorkflowReliability(null) must appear before Session expired return in catch');
});

// --- No new setInterval introduced by wiring ---
check('no new setInterval in render or load wiring', () => {
  const renderSrc = extractFunction(source, 'render');
  const loadSrc = extractFunction(source, 'load');
  assert(!renderSrc.includes('setInterval'), 'render must not contain setInterval');
  assert(!loadSrc.includes('setInterval'), 'load must not contain setInterval');
});

check('Timing breakdown: distinct elapsed, execution and gaps with coverage', () => {
  const data = recentFixture();
  data.duration = {sample_count: 3, median_seconds: 130, average_seconds: 140};
  data.job_execution_duration = {sample_count: 3, median_seconds: 30, average_seconds: 40};
  data.between_job_gaps = {sample_count: 3, median_seconds: 100, average_seconds: 100};
  data.timing_observed_jobs = 6;
  data.timing_missing_jobs = 1;
  data.timing_incomplete = false;
  const html = recentHtml(data);
  for (const expected of ['(End-to-end elapsed) Median completion: 130.0s (3 samples), avg 140.0s',
    'Job execution time: median 30.0s, avg 40.0s, 3 samples',
    'Between-job gaps: median 100.0s, avg 100.0s, 3 samples',
    '6 valid job intervals / 1 missing or invalid intervals — Incomplete',
    'not GPU-only compute', 'Gaps exclude initial queue wait', 'timestamps do not identify cause']) {
    assert(html.includes(expected), `${expected} absent: ${html}`);
  }
});
check('Timing breakdown: recorded zero gap is not missing', () => {
  const data = recentFixture();
  data.job_execution_duration = {sample_count: 3, median_seconds: 30, average_seconds: 30};
  data.between_job_gaps = {sample_count: 3, median_seconds: 0, average_seconds: 0};
  const html = recentHtml(data);
  assert(html.includes('Between-job gaps: median 0.0s, avg 0.0s, 3 samples'), html);
});
check('Timing breakdown: legacy fields missing preserve existing metrics', () => {
  const html = recentHtml(recentFixture());
  for (const expected of ['Job execution time: Not recorded', 'Between-job gaps: Not recorded',
    'Timing coverage: Unavailable', '66.7%', 'Recorded tokens: 100']) assert(html.includes(expected), html);
});
check('Timing breakdown: zero samples and invalid durations are not recorded', () => {
  for (const stats of [
    {sample_count: 0, median_seconds: 0, average_seconds: 0},
    {sample_count: true, median_seconds: 1, average_seconds: 1},
    {sample_count: 4, median_seconds: 1, average_seconds: 1},
    {sample_count: 3, median_seconds: NaN, average_seconds: Infinity},
    {sample_count: 3, median_seconds: -1, average_seconds: -2},
    {sample_count: 3, median_seconds: Number.MAX_VALUE, average_seconds: Number.MAX_VALUE},
    {sample_count: 3, median_seconds: '<img src=x onerror=bad()>', average_seconds: true},
  ]) {
    const data = recentFixture();
    data.job_execution_duration = stats;
    data.between_job_gaps = stats;
    const html = recentHtml(data);
    assert(html.includes('Job execution time: Not recorded'), html);
    assert(html.includes('Between-job gaps: Not recorded'), html);
    assert(!html.includes('<img') && !html.includes('onerror'), html);
  }
});
check('Timing breakdown: missing counts stay unavailable; actual zero coverage stays zero', () => {
  const data = recentFixture();
  data.timing_observed_jobs = 0;
  data.timing_missing_jobs = 0;
  data.timing_incomplete = true;
  let html = recentHtml(data);
  assert(html.includes('Timing coverage: 0 valid job intervals / 0 missing or invalid intervals'), html);
  assert(!html.includes('0 missing or invalid intervals — Incomplete'), html);
  for (const invalid of [null, true, -1, 1.5]) {
    data.timing_missing_jobs = invalid;
    html = recentHtml(data);
    assert(html.includes('Timing coverage: Unavailable'), html);
  }
});

// --- Workflow bottlenecks tests ---
const wbFixture = () => ({
  sample_count: 5,
  stages: [
    { stage: 'plan', job_count: 5, duration_seconds: 120, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 1000, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
    { stage: 'implement', job_count: 5, duration_seconds: 300, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 5000, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
    { stage: 'test', job_count: 5, duration_seconds: 200, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 2000, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
    { stage: 'review', job_count: 5, duration_seconds: 50, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 500, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
    { stage: 'reason', job_count: 5, duration_seconds: 30, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 300, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
    { stage: 'unknown', job_count: 5, duration_seconds: 10, timing_observed_jobs: 5, timing_missing_jobs: 0, timing_incomplete: false, total_tokens: 100, token_measured_jobs: 5, token_missing_jobs: 0, tokens_incomplete: false },
  ],
  highest_time_stage: 'implement',
  highest_token_stage: 'implement',
});
const wbHtml = (data) => {
  context.renderWorkflowBottlenecks(data);
  return $('workflow-bottlenecks').innerHTML;
};
const wbEmptyStage = (stage) => ({ stage, job_count: 0, duration_seconds: null, timing_observed_jobs: 0, timing_missing_jobs: 0, total_tokens: null, token_measured_jobs: 0, token_missing_jobs: 0 });
const wbEmptyStages = () => ['plan', 'implement', 'test', 'review', 'reason', 'unknown'].map(wbEmptyStage);

check('Workflow bottlenecks: valid data renders six stages with winners', () => {
  const html = wbHtml(wbFixture());
  assert(html.includes('Latest 5 workflows'), html);
  assert(html.includes('Highest time: implement'), html);
  assert(html.includes('Highest tokens: implement'), html);
  for (const s of ['plan', 'implement', 'test', 'review', 'reason', 'unknown']) {
    assert(html.includes(s), `${s} absent: ${html}`);
  }
  assert(html.includes('300.0s'), html);
  assert(html.includes('5000'), html);
  assert(!html.includes('Unavailable'), html);
  assert(html.includes('attempts/overlaps'), html);
  assert(html.includes('not GPU-only'), html);
  assert(html.includes('Incomplete rankings may change'), html);
});

check('Workflow bottlenecks: zero measurements display 0', () => {
  const data = wbFixture();
  for (const st of data.stages) {
    st.duration_seconds = 0;
    st.total_tokens = 0;
  }
  const html = wbHtml(data);
  assert(html.includes('Highest time: None'), html);
  assert(html.includes('Highest tokens: None'), html);
  assert(html.includes('>0<'), html);
  assert(!html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: null data shows Unavailable', () => {
  const html = wbHtml(null);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: sample_count 0 shows No recent workflows', () => {
  const html = wbHtml({ sample_count: 0, stages: wbEmptyStages() });
  assert(html.includes('No recent workflows'), html);
  assert(!html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: malformed stages (wrong count) shows Unavailable', () => {
  const data = wbFixture();
  data.stages = data.stages.slice(0, 5);
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: duplicate stage names show Unavailable', () => {
  const data = wbFixture();
  data.stages[1].stage = 'plan';
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: unknown stage name shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].stage = 'deploy';
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: wrong stage order shows Unavailable', () => {
  const data = wbFixture();
  [data.stages[0], data.stages[1]] = [data.stages[1], data.stages[0]];
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: counts inconsistency shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].timing_observed_jobs = 4;
  data.stages[0].timing_missing_jobs = 2;
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: negative duration shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].duration_seconds = -1;
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: non-integer job_count shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].job_count = 1.5;
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: NaN duration shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].duration_seconds = NaN;
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: Infinity tokens shows Unavailable', () => {
  const data = wbFixture();
  data.stages[0].total_tokens = Infinity;
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: incomplete flags derived from missing jobs', () => {
  const data = wbFixture();
  data.stages[0].timing_observed_jobs = 3;
  data.stages[0].timing_missing_jobs = 2;
  data.stages[0].timing_incomplete = false;
  const html = wbHtml(data);
  assert(html.includes('3 measured / 2 missing \u2014 Incomplete'), html);
});

check('Workflow bottlenecks: zero observed derives incomplete', () => {
  const data = wbFixture();
  data.stages[0].timing_observed_jobs = 0;
  data.stages[0].timing_missing_jobs = 5;
  data.stages[0].duration_seconds = null;
  data.stages[0].timing_incomplete = false;
  const html = wbHtml(data);
  assert(html.includes('0 measured / 5 missing \u2014 Incomplete'), html);
});

check('Workflow bottlenecks: complete data has no Incomplete flag', () => {
  const html = wbHtml(wbFixture());
  assert(!html.includes('\u2014 Incomplete'), html);
});

check('Workflow bottlenecks: winners recomputed strictly positive fixed order', () => {
  const data = wbFixture();
  data.stages[0].duration_seconds = 500;
  data.stages[1].duration_seconds = 500;
  data.highest_time_stage = 'implement';
  const html = wbHtml(data);
  assert(html.includes('Highest time: plan'), `plan should win tie by fixed order: ${html}`);
});

check('Workflow bottlenecks: zero duration excluded from winner', () => {
  const data = wbFixture();
  for (const st of data.stages) st.duration_seconds = 0;
  data.stages[2].duration_seconds = 10;
  const html = wbHtml(data);
  assert(html.includes('Highest time: test'), html);
});

check('Workflow bottlenecks: sample_count > 20 shows Unavailable', () => {
  const html = wbHtml({ sample_count: 21, stages: [] });
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: sample_count non-integer shows Unavailable', () => {
  const html = wbHtml({ sample_count: 1.5, stages: [] });
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: sample_count negative shows Unavailable', () => {
  const html = wbHtml({ sample_count: -1, stages: [] });
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: missing stages array shows Unavailable', () => {
  const html = wbHtml({ sample_count: 5 });
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: non-object data shows Unavailable', () => {
  assert(wbHtml('string').includes('Unavailable'));
  assert(wbHtml(42).includes('Unavailable'));
  assert(wbHtml([]).includes('Unavailable'));
});

check('Workflow bottlenecks: malicious stage name escaped', () => {
  const data = wbFixture();
  data.stages[0].stage = '<script>alert(1)</script>';
  const html = wbHtml(data);
  assert(html.includes('Unavailable'), html);
});

check('Workflow bottlenecks: reason/unknown empty stages render remaining rows', () => {
  const data = wbFixture();
  data.stages[4] = wbEmptyStage('reason');
  data.stages[5] = wbEmptyStage('unknown');
  const html = wbHtml(data);
  assert(!html.includes('reliability-note">Unavailable'), 'must not be whole-card Unavailable: ' + html);
  assert(html.includes('Latest 5 workflows'), html);
  assert(html.includes('>Unavailable<'), 'empty rows must show Unavailable: ' + html);
  assert(html.includes('plan'), html);
  assert(html.includes('implement'), html);
});

check('Workflow bottlenecks: partially unexecuted stage valid', () => {
  const data = wbFixture();
  data.stages[0] = { stage: 'plan', job_count: 3, duration_seconds: 5, timing_observed_jobs: 1, timing_missing_jobs: 0, total_tokens: 100, token_measured_jobs: 1, token_missing_jobs: 2 };
  const html = wbHtml(data);
  assert(!html.includes('reliability-note">Unavailable'), html);
  assert(html.includes('5.0s'), html);
  assert(html.includes('>100<'), html);
  assert(html.includes('1 measured / 0 missing'), html);
  assert(html.includes('1 measured / 2 missing \u2014 Incomplete'), html);
});

check('Workflow bottlenecks: all queued stage valid row Unavailable', () => {
  const data = wbFixture();
  data.stages[0] = { stage: 'plan', job_count: 4, duration_seconds: null, timing_observed_jobs: 0, timing_missing_jobs: 0, total_tokens: null, token_measured_jobs: 0, token_missing_jobs: 4 };
  const html = wbHtml(data);
  assert(!html.includes('reliability-note">Unavailable'), html);
  assert(html.includes('>Unavailable<'), html);
  assert(html.includes('0 measured / 0 missing'), html);
  assert(html.includes('0 measured / 4 missing \u2014 Incomplete'), html);
});

check('Workflow bottlenecks: observed 0 with non-null time rejects card', () => {
  const data = wbFixture();
  data.stages[0].timing_observed_jobs = 0;
  data.stages[0].timing_missing_jobs = 5;
  data.stages[0].duration_seconds = 120;
  const html = wbHtml(data);
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: measured 0 with non-null total rejects card', () => {
  const data = wbFixture();
  data.stages[0].token_measured_jobs = 0;
  data.stages[0].token_missing_jobs = 5;
  data.stages[0].total_tokens = 1000;
  const html = wbHtml(data);
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: measured 0 with zero value rejects card', () => {
  const data = wbFixture();
  data.stages[0].token_measured_jobs = 0;
  data.stages[0].token_missing_jobs = 5;
  data.stages[0].total_tokens = 0;
  const html = wbHtml(data);
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: observed 0 with zero duration rejects card', () => {
  const data = wbFixture();
  data.stages[0].timing_observed_jobs = 0;
  data.stages[0].timing_missing_jobs = 5;
  data.stages[0].duration_seconds = 0;
  const html = wbHtml(data);
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: float tokens rejected', () => {
  const data = wbFixture();
  data.stages[0].total_tokens = 1.5;
  const html = wbHtml(data);
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: sample 0 with missing rows invalid', () => {
  const html = wbHtml({ sample_count: 0, stages: wbEmptyStages().slice(0, 5) });
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: sample 0 with activity invalid', () => {
  const stages = wbEmptyStages();
  stages[0].job_count = 3;
  stages[0].timing_observed_jobs = 3;
  stages[0].timing_missing_jobs = 0;
  stages[0].duration_seconds = 10;
  stages[0].token_measured_jobs = 3;
  stages[0].token_missing_jobs = 0;
  stages[0].total_tokens = 100;
  const html = wbHtml({ sample_count: 0, stages });
  assert(html.includes('reliability-note">Unavailable'), html);
});

check('Workflow bottlenecks: all-zero unexecuted stage no Incomplete', () => {
  const data = wbFixture();
  data.stages[0] = { stage: 'plan', job_count: 3, duration_seconds: null, timing_observed_jobs: 0, timing_missing_jobs: 0, total_tokens: null, token_measured_jobs: 0, token_missing_jobs: 3 };
  const html = wbHtml(data);
  assert(!html.includes('reliability-note">Unavailable'), html);
  assert(html.includes('0 measured / 0 missing'), html);
  assert(!html.includes('0 measured / 0 missing \u2014 Incomplete'), html);
});

// --- Wiring: render calls renderWorkflowBottlenecks ---
check('render calls renderWorkflowBottlenecks(data.workflow_bottlenecks)', () => {
  const renderSrc = extractFunction(source, 'render');
  assert(renderSrc !== null, 'render function must exist');
  assert(renderSrc.includes('renderWorkflowBottlenecks(data.workflow_bottlenecks)'),
    'render must call renderWorkflowBottlenecks(data.workflow_bottlenecks)');
});

// --- Wiring: load catch calls renderWorkflowBottlenecks(null) before Session expired return ---
check('load catch calls renderWorkflowBottlenecks(null) before Session expired return', () => {
  const loadSrc = extractFunction(source, 'load');
  assert(loadSrc !== null, 'load function must exist');
  const catchIdx = loadSrc.indexOf('catch');
  assert(catchIdx !== -1, 'load must have a catch block');
  const catchSrc = loadSrc.slice(catchIdx);
  const nullCallIdx = catchSrc.indexOf('renderWorkflowBottlenecks(null)');
  assert(nullCallIdx !== -1, 'load catch must call renderWorkflowBottlenecks(null)');
  const sessionIdx = catchSrc.indexOf("Session expired");
  assert(sessionIdx !== -1, 'load catch must reference Session expired');
  assert(nullCallIdx < sessionIdx, 'renderWorkflowBottlenecks(null) must appear before Session expired return in catch');
});

// --- renderCompletionRecovery tests ---
check('renderCompletionRecovery: valid counts renders data', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 15, implementer_jobs: 15, observed_jobs: 12,
    unknown_jobs: 3, attempted_jobs: 5, recovered_jobs: 3,
    failed_jobs: 2, not_attempted_jobs: 7,
  });
  assert(el.innerHTML.includes('15'), 'should show sample count');
  assert(el.innerHTML.includes('60%'), 'should show 60% rate');
  assert(el.innerHTML.includes('Incomplete'), 'should show incomplete warning');
});

check('renderCompletionRecovery: rate 0% when all failed', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 10, implementer_jobs: 10, observed_jobs: 10,
    unknown_jobs: 0, attempted_jobs: 10, recovered_jobs: 0,
    failed_jobs: 10, not_attempted_jobs: 0,
  });
  assert(el.innerHTML.includes('0%'), 'should show 0% rate');
  assert(!el.innerHTML.includes('Incomplete'), 'should not show incomplete');
});

check('renderCompletionRecovery: no attempts shows Not attempted', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 10, implementer_jobs: 10, observed_jobs: 10,
    unknown_jobs: 0, attempted_jobs: 0, recovered_jobs: 0,
    failed_jobs: 0, not_attempted_jobs: 10,
  });
  assert(el.innerHTML.includes('Not attempted'), 'should show Not attempted');
  assert(!el.innerHTML.includes('0%'), 'should not show 0%');
  assert(!el.innerHTML.includes('100%'), 'should not show 100%');
});

check('renderCompletionRecovery: unknown warning', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 5, implementer_jobs: 5, observed_jobs: 0,
    unknown_jobs: 5, attempted_jobs: 0, recovered_jobs: 0,
    failed_jobs: 0, not_attempted_jobs: 0,
  });
  assert(el.innerHTML.includes('Incomplete'), 'should show incomplete warning for unknown');
});

check('renderCompletionRecovery: empty sample shows No recent workflows', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 0, implementer_jobs: 0, observed_jobs: 0,
    unknown_jobs: 0, attempted_jobs: 0, recovered_jobs: 0,
    failed_jobs: 0, not_attempted_jobs: 0,
  });
  assert(el.innerHTML.includes('No recent workflows'), 'should show No recent workflows');
});

check('renderCompletionRecovery: null shows Unavailable', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery(null);
  assert(el.innerHTML.includes('Unavailable'), 'should show Unavailable for null');
});

check('renderCompletionRecovery: bad counts shows Unavailable', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 15, implementer_jobs: 15, observed_jobs: 12,
    unknown_jobs: 3, attempted_jobs: 5, recovered_jobs: 3,
    failed_jobs: 2, not_attempted_jobs: 8, // invariant violation: 12 != 8+5
  });
  assert(el.innerHTML.includes('Unavailable'), 'should show Unavailable for bad invariants');
});

check('renderCompletionRecovery: injected metadata is escaped', () => {
  const el = $('completion-recovery');
  renderCompletionRecovery({
    sample_count: 15, implementer_jobs: 15, observed_jobs: 12,
    unknown_jobs: 3, attempted_jobs: 5, recovered_jobs: 3,
    failed_jobs: 2, not_attempted_jobs: 7,
    metadata: '<script>alert("xss")</script>',
  });
  assert(!el.innerHTML.includes('<script>'), 'should not contain raw script tag');
});

check('renderCompletionRecovery: wiring in render()', () => {
  const src = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');
  assert(src.includes('renderCompletionRecovery(data.completion_recovery_summary)'), 'render must call renderCompletionRecovery');
});

check('renderCompletionRecovery: wiring in load catch', () => {
  const src = extractFunction(source, 'load');
  assert(src !== null, 'load function must exist');
  const catchIdx = src.indexOf('catch(error)');
  const catchBlock = src.slice(catchIdx, catchIdx + 300);
  assert(catchBlock.includes('renderCompletionRecovery(null)'), 'load catch must clear completion recovery');
  const sessionIdx = catchBlock.indexOf('Session expired');
  const nullCallIdx = catchBlock.indexOf('renderCompletionRecovery(null)');
  assert(nullCallIdx >= 0 && sessionIdx >= 0, 'clear and session-expired branches must both exist');
  assert(nullCallIdx < sessionIdx, 'renderCompletionRecovery(null) must appear before Session expired return');
});

const crFixture = () => ({sample_count: 15, implementer_jobs: 15, observed_jobs: 12,
  unknown_jobs: 3, attempted_jobs: 5, recovered_jobs: 3, failed_jobs: 2, not_attempted_jobs: 7});
const crHtml = (data) => { renderCompletionRecovery(data); return $('completion-recovery').innerHTML; };
check('Completion recovery: unsafe counts and malformed shapes rejected with heading', () => {
  for (const key of Object.keys(crFixture())) {
    for (const bad of [true, false, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, '1', null]) {
      const data = crFixture(); data[key] = bad;
      const html = crHtml(data);
      assert(html.includes('Unavailable') && html.includes('<h2>Completion recovery</h2>'), html);
    }
  }
  for (const data of [null, [], 'legacy', {}, {sample_count: 0}]) {
    assert(crHtml(data).includes('Unavailable'));
  }
});
check('Completion recovery: valid, empty and unavailable retain heading; rate and warning derived', () => {
  const data = crFixture(); data.recovery_success_rate = '<img onerror=alert(1)>'; data.incomplete = false;
  let html = crHtml(data);
  assert(html.includes('60%') && html.includes('Incomplete') && !html.includes('<img'), html);
  assert(html.includes('Latest 15 workflows (up to 20)'), html);
  assert(html.includes('Missing historical telemetry is not zero') && html.includes('not proof of code correctness'), html);
  const empty = Object.fromEntries(Object.keys(crFixture()).map(key => [key, 0]));
  html = crHtml(empty);
  assert(html.includes('No recent workflows') && html.includes('<h2>Completion recovery</h2>'), html);
  html = crHtml(null);
  assert(html.includes('Unavailable') && html.includes('<h2>Completion recovery</h2>'), html);
});
check('Completion recovery: unknown-only never claims a measured recovery rate', () => {
  const data = {sample_count: 1, implementer_jobs: 3, observed_jobs: 0, unknown_jobs: 3,
    attempted_jobs: 0, recovered_jobs: 0, failed_jobs: 0, not_attempted_jobs: 0};
  const html = crHtml(data);
  assert(html.includes('Unknown: 3') && html.includes('Not attempted') && html.includes('Incomplete'), html);
  assert(!html.includes('0%') && !html.includes('100%'), html);
});
check('Completion recovery: each accounting invariant and empty-window activity fail closed', () => {
  for (const key of ['implementer_jobs', 'attempted_jobs', 'observed_jobs', 'sample_count']) {
    const data = crFixture(); data[key] = key === 'sample_count' ? 0 : data[key] + 1;
    assert(crHtml(data).includes('Unavailable'), key);
  }
});

// --- renderValidationDiagnostics tests ---
const { renderValidationDiagnostics: vdFn } = context;
assert(typeof vdFn === 'function', 'renderValidationDiagnostics must be defined in context');

function vdHtml() { return $('validation-diagnostics-body').innerHTML; }

check('Validation diagnostics: null => Unavailable', () => {
  vdFn(null);
  assert(vdHtml().includes('Unavailable'), 'null should show Unavailable');
});

check('Validation diagnostics: undefined => Unavailable', () => {
  vdFn(undefined);
  assert(vdHtml().includes('Unavailable'), 'undefined should show Unavailable');
});

check('Validation diagnostics: non-array => Unavailable', () => {
  vdFn('garbage');
  assert(vdHtml().includes('Unavailable'), 'non-array should show Unavailable');
});

check('Validation diagnostics: empty array => No recent validation jobs', () => {
  vdFn([]);
  assert(vdHtml().includes('No recent validation jobs'), 'empty should show message');
});

check('Validation diagnostics: valid implementer row renders', () => {
  vdFn([{role:'implementer',stage:'build',project:'alpha',validation_request_count:10,validation_execution_count:8,validation_last_outcome:'passed',validation_recovery_attempted:1,validation_recovery_outcome:'recovered'}]);
  const h = vdHtml();
  assert(h.includes('build/alpha'), 'stage/project label');
  assert(h.includes('10'), 'requests=10');
  assert(h.includes('8'), 'executions=8');
  assert(h.includes('Passed'), 'outcome label');
  assert(h.includes('Recovered'), 'recovery label');
});

check('Validation diagnostics: zero requests => Not attempted', () => {
  vdFn([{role:'implementer',stage:'s',project:'p',validation_request_count:0,validation_execution_count:0,validation_last_outcome:'not_attempted',validation_recovery_attempted:0,validation_recovery_outcome:'not_attempted'}]);
  const h = vdHtml();
  assert(h.includes('0'), 'explicit 0 display');
  assert(h.includes('Not attempted'), 'not_attempted label');
});

check('Validation diagnostics: missing fields => Not recorded', () => {
  vdFn([{role:'implementer',stage:'s',project:'p'}]);
  const h = vdHtml();
  assert(h.includes('Not recorded'), 'missing outcome => Not recorded');
});

check('Validation diagnostics: tester recovery => Not applicable', () => {
  vdFn([{role:'tester',stage:'s',project:'p',validation_request_count:5,validation_execution_count:3,validation_last_outcome:'passed',validation_recovery_attempted:1,validation_recovery_outcome:'recovered'}]);
  const h = vdHtml();
  assert(h.includes('Not applicable'), 'tester recovery => Not applicable');
});

check('Validation diagnostics: malicious injection is escaped', () => {
  vdFn([{role:'implementer',stage:'<script>alert(1)</script>',project:'<img src=x onerror=alert(2)>',validation_request_count:1,validation_execution_count:1,validation_last_outcome:'passed',validation_recovery_attempted:0,validation_recovery_outcome:'not_attempted'}]);
  const h = vdHtml();
  assert(!h.includes('<script>'), 'script tag escaped');
  assert(!h.includes('<img'), 'img tag escaped');
});

check('Validation diagnostics: caps at 20 rows', () => {
  const jobs = Array.from({length:25},(_,i)=>({role:'implementer',stage:'s'+i,project:'p',validation_request_count:1,validation_execution_count:1,validation_last_outcome:'passed',validation_recovery_attempted:0,validation_recovery_outcome:'not_attempted'}));
  vdFn(jobs);
  const rowCount = (vdHtml().match(/<tr>/g) || []).length - 1; // subtract header
  assert(rowCount === 20, `expected 20 rows, got ${rowCount}`);
});

check('Validation diagnostics: invalid triple => Not recorded', () => {
  vdFn([{role:'implementer',stage:'s',project:'p',validation_request_count:10,validation_execution_count:20,validation_last_outcome:'passed',validation_recovery_attempted:0,validation_recovery_outcome:'not_attempted'}]);
  const h = vdHtml();
  assert(h.includes('Not recorded'), 'exec>req invalid => Not recorded');
});

check('Validation diagnostics: render wiring in render()', () => {
  const src = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');
  assert(src.includes('renderValidationDiagnostics(data.jobs)'), 'render calls renderValidationDiagnostics');
  assert(src.includes('renderValidationDiagnostics(null)'), 'load catch calls renderValidationDiagnostics(null)');
});

check('Validation diagnostics: HTML root inside existing wrapper', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
  assert(html.includes('id="validation-diagnostics"'), 'root id present');
  assert(html.includes('Reliability and diagnostics'), 'inside reliability section');
  const relIdx = html.indexOf('id="reliability-section"');
  const vdIdx = html.indexOf('id="validation-diagnostics"');
  assert(relIdx !== -1 && vdIdx !== -1 && vdIdx > relIdx, 'validation-diagnostics inside reliability-section');
});

check('Validation diagnostics: deployed gateway contract fixture', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/validation_dashboard.json'), 'utf8'));
  const before = JSON.stringify(fixture);
  vdFn(fixture.jobs);
  const h = vdHtml();
  assert(h.includes('implement/fixture') && h.includes('test/fixture'));
  assert(h.includes('<td>4</td><td>2</td><td>Suppressed</td><td>Recovered</td>'));
  assert(h.includes('<td>0</td><td>0</td><td>Not attempted</td><td>Not applicable</td>'));
  assert(h.includes('Not recorded'));
  assert(!h.includes('Unavailable'));
  assert.strictEqual(JSON.stringify(fixture), before);
});

check('Validation diagnostics: roles filtered BEFORE the 20-row cap', () => {
  const irrelevant = Array.from({length:25}, () => ({role:'planner',stage:'SECRET_PLANNER'}));
  vdFn([...irrelevant, null, 42, {role:'implementer',stage:'visible-implementer'}]);
  assert(vdHtml().includes('visible-implementer'));
  assert(!vdHtml().includes('SECRET_PLANNER'));
  assert.strictEqual((vdHtml().match(/<tr>/g) || []).length, 2);
  vdFn(irrelevant);
  assert(vdHtml().includes('No recent validation jobs'));
});

check('Validation diagnostics: invalid telemetry is never displayed as recorded', () => {
  const good = {role:'implementer',stage:'fixture',validation_request_count:3,
    validation_execution_count:2,validation_last_outcome:'passed',
    validation_recovery_attempted:1,validation_recovery_outcome:'recovered'};
  for (const patch of [
    {validation_request_count:true}, {validation_execution_count:false},
    {validation_request_count:'3'}, {validation_execution_count:1.5},
    {validation_request_count:1000001}, {validation_execution_count:-1},
    {validation_execution_count:4}, {validation_last_outcome:'<img SECRET>'},
    {validation_request_count:0,validation_execution_count:0},
    {validation_execution_count:0}, {validation_last_outcome:'not_attempted'},
  ]) {
    vdFn([{...good,...patch}]);
    assert(vdHtml().includes('<td>—</td><td>—</td><td>Not recorded</td><td>Recovered</td>'));
    assert(!vdHtml().includes('SECRET'));
  }
  for (const patch of [
    {validation_recovery_attempted:true}, {validation_recovery_attempted:'1'},
    {validation_recovery_attempted:0}, {validation_recovery_outcome:'not_attempted'},
    {validation_recovery_outcome:'<script>SECRET</script>'},
  ]) {
    vdFn([{...good,...patch}]);
    assert(vdHtml().includes('<td>3</td><td>2</td><td>Passed</td><td>Not recorded</td>'));
    assert(!vdHtml().includes('SECRET'));
  }
});

check('Validation diagnostics: stable unique heading and actual details containment', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
  const start = html.indexOf('id="reliability-section"');
  const end = html.indexOf('</details>', start);
  const root = html.indexOf('id="validation-diagnostics"');
  assert(root > start && root < end);
  assert.strictEqual((html.match(/id="validation-diagnostics-title"/g) || []).length, 1);
  vdFn([{role:'implementer'}]);
  assert(!vdHtml().includes('id="validation-diagnostics-title"'));
  const renderSource = extractFunction(source, 'render');
  const loadSource = extractFunction(source, 'load');
  assert(renderSource.includes('renderValidationDiagnostics(data.jobs)'));
  assert(!renderSource.includes('recent_jobs'));
  assert(loadSource.indexOf('renderValidationDiagnostics(null)') < loadSource.indexOf("if(error.message==='Session expired') return"));
});

// --- renderAgentActivity tests ---

check('renderAgentActivity: valid multi-project counts sums running and queued', () => {
  renderAgentActivity([
    {project: 'alpha', status: 'running', count: 3},
    {project: 'alpha', status: 'queued', count: 2},
    {project: 'beta', status: 'running', count: 1},
    {project: 'beta', status: 'queued', count: 4},
  ]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('>4<')); // 3+1 running
  assert(el.innerHTML.includes('>6<')); // 2+4 queued
  assert(el.innerHTML.includes('Occupied slots reflect running jobs; total worker capacity is not reported.'));
});

check('renderAgentActivity: empty counts shows zeros not Unavailable', () => {
  renderAgentActivity([]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('>0<'));
  assert(!el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: null counts shows Unavailable', () => {
  renderAgentActivity(null);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: undefined counts shows Unavailable', () => {
  renderAgentActivity(undefined);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: non-array object shows Unavailable', () => {
  renderAgentActivity({running: 3});
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: string input shows Unavailable', () => {
  renderAgentActivity('not an array');
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: negative count shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running', count: -1}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: boolean count shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running', count: true}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: string count shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running', count: '3'}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: fractional count shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running', count: 1.5}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: unsafe integer count shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running', count: Number.MAX_SAFE_INTEGER + 1}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: overflow sum shows Unavailable', () => {
  renderAgentActivity([
    {project: 'a', status: 'running', count: Number.MAX_SAFE_INTEGER},
    {project: 'b', status: 'running', count: 1},
  ]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: null entry in array shows Unavailable', () => {
  renderAgentActivity([null]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: missing status field shows Unavailable', () => {
  renderAgentActivity([{project: 'a', count: 3}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: missing count field shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 'running'}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: non-string status shows Unavailable', () => {
  renderAgentActivity([{project: 'a', status: 123, count: 3}]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: array entry shows Unavailable', () => {
  renderAgentActivity([[1, 2, 3]]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('Unavailable'));
});

check('renderAgentActivity: unrelated statuses ignored in sum', () => {
  renderAgentActivity([
    {project: 'a', status: 'completed', count: 10},
    {project: 'a', status: 'failed', count: 5},
    {project: 'a', status: 'running', count: 2},
    {project: 'a', status: 'queued', count: 1},
  ]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('>2<')); // running
  assert(el.innerHTML.includes('>1<')); // queued
  // completed and failed should not appear as values
  assert(!el.innerHTML.includes('>10<'));
  assert(!el.innerHTML.includes('>5<'));
});

check('renderAgentActivity: running count greater than 2 not clamped', () => {
  renderAgentActivity([
    {project: 'a', status: 'running', count: 5},
    {project: 'b', status: 'running', count: 3},
  ]);
  const el = $('agent-activity-body');
  assert(el.innerHTML.includes('>8<')); // 5+3=8, not clamped to 2
});

check('renderAgentActivity: does not derive from recent jobs', () => {
  const fnSource = extractFunction(source, 'renderAgentActivity');
  assert(!fnSource.includes('data.jobs'));
  assert(!fnSource.includes('recent_jobs'));
  assert(!fnSource.includes('recent60sample'));
});

check('renderAgentActivity: stable unique heading in HTML', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
  assert(html.includes('id="agent-activity-title"'));
  assert.strictEqual((html.match(/id="agent-activity-title"/g) || []).length, 1);
  assert(html.includes('Agent activity summary'));
});

check('renderAgentActivity: render(data.counts) wiring in render function', () => {
  const renderSource = extractFunction(source, 'render');
  assert(renderSource.includes('renderAgentActivity(counts)'));
});

check('renderAgentActivity: load error clears before Session return', () => {
  const loadSource = extractFunction(source, 'load');
  assert(loadSource.indexOf('renderAgentActivity(null)') < loadSource.indexOf("if(error.message==='Session expired') return"));
});

check('renderAgentActivity: original Running now unchanged in render', () => {
  const renderSource = extractFunction(source, 'render');
  assert(renderSource.includes('renderRunningNow(jobs)'));
});

check('renderAgentActivity: HTML root near Running now and accessible', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
  const runningIdx = html.indexOf('id="running-now"');
  const agentIdx = html.indexOf('id="agent-activity"');
  assert(runningIdx > -1);
  assert(agentIdx > -1);
  assert(agentIdx > runningIdx);
  assert(html.includes('aria-labelledby="agent-activity-title"'));
  assert.strictEqual((html.match(/id="agent-activity"/g) || []).length, 1);
  assert.strictEqual((html.match(/id="agent-activity-body"/g) || []).length, 1);
});

check('renderAgentActivity: no capacity hardcoding or clamping in source', () => {
  const fnSource = extractFunction(source, 'renderAgentActivity');
  assert(!fnSource.includes('Math.min'));
  assert(!fnSource.includes('Math.max'));
  assert(!/capacity\s*=\s*2/.test(fnSource));
  assert(!/clamp/.test(fnSource));
  assert(fnSource.includes('Occupied slots reflect running jobs; total worker capacity is not reported.'));
});

check('renderAgentActivity: no alert status text implying heartbeat proof', () => {
  const fnSource = extractFunction(source, 'renderAgentActivity');
  assert(!fnSource.includes('heartbeat'));
  assert(!fnSource.includes('alive'));
  assert(!fnSource.includes('healthy'));
});

console.log(`\n${passed}/${total} checks passed`);
process.exit(0);
