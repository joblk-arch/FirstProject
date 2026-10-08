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
const REQUIRED = ['renderClusterHealth', 'renderStatusCountsCard', 'renderClusterHealthError'];
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

const { renderClusterHealth, renderStatusCountsCard, renderClusterHealthError } = context;
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

// --- Test 1: agent_queue.status_counts wiring via renderClusterHealth ---
check('renderClusterHealth wires agent_queue.status_counts into the reliability card', () => {
  renderClusterHealth({
    overall: 'healthy',
    generated_at: '2024-01-01T00:00:00Z',
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
  renderStatusCountsCard({ completed: 5, failed: 2, blocked: 7 });
  const card = $('reliability-card').innerHTML;
  assert(card.includes('<strong>5</strong>'), card);
  assert(card.includes('<strong>2</strong>'), card);
  assert(card.includes('<strong>7</strong>'), card);
  assert(!card.includes('Unavailable'), card);
});

check('zero counts render as 0 (not Unavailable)', () => {
  renderStatusCountsCard({ completed: 0, failed: 0, blocked: 0 });
  const card = $('reliability-card').innerHTML;
  assert.strictEqual((card.match(/<strong>0<\/strong>/g) || []).length, 3, card);
  assert(!card.includes('Unavailable'), card);
});

// --- Test 3: null / bool / negative / oversized / malicious -> Unavailable, no injected markup ---
check('null counts show Unavailable', () => {
  renderStatusCountsCard({ completed: null, failed: null, blocked: null });
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('boolean counts show Unavailable', () => {
  renderStatusCountsCard({ completed: true, failed: false, blocked: true });
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('negative counts show Unavailable', () => {
  renderStatusCountsCard({ completed: -1, failed: -5, blocked: -100 });
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('oversized counts (> 10,000,000) show Unavailable', () => {
  renderStatusCountsCard({ completed: 10000001, failed: 1e9, blocked: 100000000 });
  assert.strictEqual(countUnavailable($('reliability-card').innerHTML), 3);
});

check('count == _SAFE_BOUND (10,000,000) renders as a value, not Unavailable', () => {
  renderStatusCountsCard({ completed: 10000000, failed: 10000000, blocked: 10000000 });
  const card = $('reliability-card').innerHTML;
  assert.strictEqual(countUnavailable(card), 0, card);
  assert.strictEqual((card.match(/<strong>10000000<\/strong>/g) || []).length, 3, card);
});

check('malicious strings show Unavailable without injected markup', () => {
  renderStatusCountsCard({
    completed: '<script>alert("xss")</script>',
    failed: '<img src=x onerror=alert(1)>',
    blocked: '"><svg onload=alert(2)>',
  });
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
    generated_at: '2024-01-01T00:00:00Z',
    services: [],
    lm_studio: {},
    agent_queue: { running: 1, queued: 0, status_counts: { completed: 42, failed: 7, blocked: 2 } },
  });
  let card = $('reliability-card').innerHTML;
  assert(card.includes('<strong>42</strong>'), `expected 42 before error: ${card}`);
  assert(!card.includes('Unavailable'), `unexpected Unavailable before error: ${card}`);

  renderClusterHealthError();
  card = $('reliability-card').innerHTML;
  assert.strictEqual(countUnavailable(card), 3, `expected all Unavailable after error: ${card}`);
  assert(!card.includes('<strong>42</strong>'), `stale count 42 survived error: ${card}`);
  assert(!card.includes('<strong>7</strong>'), `stale count 7 survived error: ${card}`);
  assert(!card.includes('<strong>2</strong>'), `stale count 2 survived error: ${card}`);
});

console.log(`\n${passed}/${total} checks passed`);
process.exit(0);
