const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.text = ''; }
  appendChild(node) { this.children.push(node); }
  replaceChildren() { this.children = []; this.text = ''; }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(' '); }
}
const root = new Node('div');
const context = {document: {getElementById: id => id === 'isolated-workflow-status' ? root : null, createElement: tag => {
  assert(!['button', 'a', 'form', 'input'].includes(tag)); return new Node(tag);
}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../static/isolated-workflows.js'), 'utf8'), context);
const source = {label: '<img onerror=attack>', read_only: true, availability: 'available', jobs: [{role: 'planner', status: 'running', id: 'a97bf857edcf'}]};
context.renderIsolatedWorkflows({read_only: true, sources: [source]});
assert(root.textContent.includes('planner: running'));
assert(root.textContent.includes('<img onerror=attack>')); // text node, not HTML
context.renderIsolatedWorkflows({read_only: true, sources: [source]});
assert.strictEqual(root.children.length, 1); // refresh replaces, never duplicates
context.renderIsolatedWorkflows(null);
assert(root.textContent.includes('unavailable') && !root.textContent.includes('running'));
context.renderIsolatedWorkflows({read_only: true, sources: [{...source, availability: 'unavailable'}]});
assert(root.textContent.includes('Current status unavailable') && !root.textContent.includes('planner: running'));
assert(!('fetch' in context));
const build = (label, statuses) => ({label, read_only: true, availability: 'available',
  jobs: statuses.map((status, i) => ({role: ['planner','implementer','tester','reviewer'][i], status, id: label+i}))});
const old = build('Historical', ['completed','completed','completed','completed']);
const current = build('Current', ['completed','running','queued','queued']);
const snapshot = JSON.stringify([old, current]);
context.renderIsolatedWorkflows({read_only: true, sources: [old, current]});
assert(root.textContent.includes('Current') && root.textContent.includes('implementer: running'));
assert(!root.textContent.includes('Historical'));
assert.strictEqual(root.children.length, 1);
assert.strictEqual(JSON.stringify([old, current]), snapshot); // display only, no input mutation
context.renderIsolatedWorkflows({read_only: true, sources: [old]});
assert.strictEqual(root.textContent, 'No active isolated builds.');
const stopped = build('Stopped', ['completed','failed','blocked','queued']);
context.renderIsolatedWorkflows({read_only: true, sources: [old, stopped]});
assert.strictEqual(root.textContent, 'No active isolated builds.');
const queued = build('Next', ['queued','queued','queued','queued']);
context.renderIsolatedWorkflows({read_only: true, sources: [old, queued]});
assert(root.textContent.includes('Next') && !root.textContent.includes('Historical'));
context.renderIsolatedWorkflows({read_only: true, sources: [current, queued]});
assert(root.textContent.includes('Current') && !root.textContent.includes('Next'));
const newer = build('Newest', ['completed','completed','running','queued']);
context.renderIsolatedWorkflows({read_only: true, sources: [current, newer]});
assert(root.textContent.includes('Newest') && !root.textContent.includes('Current'));
assert.strictEqual(root.children.length, 1);
context.renderIsolatedWorkflows({read_only: true, sources: []});
assert.strictEqual(root.textContent, 'No active isolated builds.');
assert(fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8').includes('/isolated-workflows.js?v=current-only-2'));
console.log('isolated read-only renderer passed');
