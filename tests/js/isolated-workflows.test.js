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
console.log('isolated read-only renderer passed');
