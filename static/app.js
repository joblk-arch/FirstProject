const $ = (id) => document.getElementById(id);
const fmt = (n) => new Intl.NumberFormat().format(n || 0);
const duration = (n) => n == null ? '\u2014' : n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${n % 60}s`;
const safe = (s) => String(s ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const ROUTING_REASON_LABELS = {
  'explicit-fast': 'Fast selected',
  'explicit-deep': 'Deep selected',
  'reasoning-keyword': 'Reasoning task',
  'long-complex-prompt': 'Complex prompt',
  'default-fast': 'Fast default',
  'reasoner-default-deep': 'Reasoner default',
  'auto-mutation-stays-fast': 'Tool-safe fast',
};
const routingReason = (reason) => ROUTING_REASON_LABELS[reason] || '\u2014';
const modelWithReason = (model, reason) => `${safe(model || 'Not recorded')}<small class="model-reason">${safe(routingReason(reason))}</small>`;
const dialog = $('workflow-dialog');
const workflowContent = $('workflow-content');
let activeWorkflowId = null;
let sessionIdentity = {username:'',role:'viewer',csrf_token:null};
let buildKey = null;
let buildKeyIntent = null;
let buildInFlight = false;

function repairBadge(state, attempts, max) {
  if (!state || state === 'none') return '<span class="repair-badge repair-none" aria-label="No repair activity">\u2014</span>';
  const cls = `repair-badge repair-${state.replace(/_/g, '-')}`;
  let label;
  if (state === 'repairing') {
    label = attempts != null && max != null ? `Repairing ${attempts}/${max}` : 'Repairing';
  } else if (state === 'ready_after_repair') {
    label = attempts != null ? `Ready after ${attempts} repair${attempts !== 1 ? 's' : ''}` : 'Ready after repair';
  } else if (state === 'rejected_attempts_remaining') {
    label = attempts != null && max != null ? `Rejected, ${max - attempts} attempt${max - attempts !== 1 ? 's' : ''} left` : 'Rejected, attempts remaining';
  } else if (state === 'exhausted') {
    label = max != null ? `Exhausted at ${max}` : 'Exhausted';
  } else if (state === 'history') {
    label = attempts != null ? `${attempts} repair${attempts !== 1 ? 's' : ''} used` : 'Repair history';
  } else {
    label = safe(state);
  }
  return `<span class="${cls}" role="status" aria-label="${safe(label)}">${safe(label)}</span>`;
}

function render(data) {
  const jobs = data.jobs || [], usage = data.usage || [], counts = data.counts || [];
  const active = jobs.filter((j) => ['queued', 'running'].includes(j.status)).length;
  const failed = jobs.filter((j) => ['failed', 'blocked'].includes(j.status)).length;
  const tokens = usage.reduce((n, x) => n + (x.total_tokens || 0), 0);
  const repairEnabled = data.repair_enabled === true;
  const repairActivated = data.repair_activated === true;
  const repairMax = data.repair_max_attempts != null ? data.repair_max_attempts : null;
  const repairSystemLabel = repairEnabled ? (repairActivated ? `Active${repairMax != null ? ` (max ${repairMax})` : ''}` : 'Enabled') : 'Off';
  const repairSystemCls = repairEnabled ? (repairActivated ? 'repair-active' : 'repair-enabled') : 'repair-off';
  const repairSystem = `<article class="metric"><span>Auto-repair</span><strong class="repair-system ${repairSystemCls}">${safe(repairSystemLabel)}</strong></article>`;
  $('metrics').innerHTML = [['Active work',active],['Projects',data.projects.length],['Recorded tokens',fmt(tokens)],['Needs attention',failed]].map(([key,value]) => `<article class="metric"><span>${key}</span><strong>${value}</strong></article>`).join('') + repairSystem;
  $('jobs').innerHTML = jobs.length ? jobs.slice(0,20).map((job) => {
    const label = safe(job.stage || job.role);
    const stage = job.workflow_id ? `<button class="workflow-link" type="button" data-workflow-id="${safe(job.workflow_id)}" aria-label="Open ${label} workflow details">${label}</button>` : label;
    return `<tr><td>${stage}</td><td>${safe(job.project)}</td><td><span class="status ${safe(job.status)}">${safe(job.status)}</span></td><td>${modelWithReason(job.model, job.model_reason)}</td><td>${job.total_tokens == null ? '\u2014' : fmt(job.total_tokens)}</td><td>${duration(job.duration_seconds)}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">No agent jobs yet.</td></tr>';
  const byProject = Object.groupBy ? Object.groupBy(counts,(x) => x.project) : counts.reduce((all,x) => ((all[x.project] ??= []).push(x),all),{});
  $('projects').innerHTML = data.projects.map((project) => { const rows=byProject[project.name]||[]; const total=rows.reduce((n,x)=>n+x.count,0); const running=rows.filter((x)=>['running','queued'].includes(x.status)).reduce((n,x)=>n+x.count,0); const width=Math.min(100,total?Math.max(5,running/total*100):0); return `<div class="item"><div class="item-row"><strong>${safe(project.name)}</strong><span>${total} jobs</span></div><small>${safe(project.default_branch)} \u00b7 ${running} active</small><div class="bar"><i style="width:${width}%"></i></div></div>`; }).join('');
  $('models').innerHTML = usage.length ? usage.map((item) => `<div class="item"><div class="item-row"><strong>${safe((item.model||'Unknown').split('/').pop())}</strong><span>${fmt(item.total_tokens)}</span></div><small>${safe(item.project)} \u00b7 ${item.jobs} jobs \u00b7 ${fmt(item.prompt_tokens)} in / ${fmt(item.completion_tokens)} out</small></div>`).join('') : '<div class="empty">Token accounting begins with the next agent job.</div>';
  const workflows = Array.isArray(data.recent_workflows) ? data.recent_workflows : [];
  const sorted = workflows.slice().sort((a, b) => {
    const ta = Date.parse(a.created_at) || 0;
    const tb = Date.parse(b.created_at) || 0;
    return tb - ta;
  });
  $('workflows').innerHTML = sorted.length ? sorted.map((w) => {
    const id = w.id ? safe(w.id) : '';
    const objective = w.objective != null ? safe(w.objective) : '\u2014';
    const status = w.status != null ? safe(w.status) : 'unknown';
    const repair = repairBadge(w.repair_state, w.repair_attempts, w.repair_max_attempts);
    const origin = w.origin != null ? safe(w.origin) : '\u2014';
    const models = Array.isArray(w.models) ? w.models.map((m) => safe(m)).join(', ') : (w.models != null ? safe(w.models) : '\u2014');
    const modelReasons = Array.isArray(w.model_reasons) && w.model_reasons.length ? w.model_reasons.map(routingReason).map(safe).join(', ') : '\u2014';
    const stageCounts = w.stage_counts != null ? (typeof w.stage_counts === 'object' ? Object.entries(w.stage_counts).map(([k, v]) => `${safe(k)}:${safe(v)}`).join(' ') : safe(w.stage_counts)) : '\u2014';
    const tokens = w.total_tokens == null ? '\u2014' : `${fmt(w.prompt_tokens)} / ${fmt(w.completion_tokens)} / ${fmt(w.total_tokens)}`;
    const created = w.created_at ? new Date(w.created_at).toLocaleString() : '\u2014';
    const label = w.project != null ? safe(w.project) : 'Unknown';
    const link = id ? `<button class="workflow-link" type="button" data-workflow-id="${id}" aria-label="Open ${label} workflow details">${label}</button>` : label;
    return `<tr><td>${link}</td><td>${objective}</td><td><span class="status ${status}">${status}</span></td><td>${repair}</td><td>${origin}</td><td>${models}<small class="model-reason">${modelReasons}</small></td><td>${stageCounts}</td><td>${tokens}</td><td>${created}</td></tr>`;
  }).join('') : '<tr><td colspan="9" class="empty">No workflows recorded yet.</td></tr>';
  $('updated').textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
  $('error').hidden = true;
}

function stageCard(stage,index) {
  const tokens = stage.total_tokens == null ? 'Not recorded' : `${fmt(stage.total_tokens)} total`;
  const report = stage.report ? `<details><summary>Stage report</summary><pre class="report">${safe(stage.report)}</pre></details>` : '<p class="muted-copy">No stage report available.</p>';
  return `<article class="stage-card"><div class="stage-order" aria-hidden="true">${index+1}</div><div class="stage-body"><div class="stage-head"><div><span class="stage-role">${safe(stage.stage||stage.role)}</span><strong>${safe(stage.role||'agent')}</strong></div><span class="status ${safe(stage.status)}">${safe(stage.status)}</span></div><dl class="stage-meta"><div><dt>Duration</dt><dd>${duration(stage.duration_seconds)}</dd></div><div><dt>Model</dt><dd>${modelWithReason(stage.model, stage.model_reason)}</dd></div><div><dt>Tokens</dt><dd>${tokens}</dd></div><div><dt>Input / output</dt><dd>${stage.prompt_tokens==null?'\u2014':`${fmt(stage.prompt_tokens)} / ${fmt(stage.completion_tokens)}`}</dd></div></dl>${report}</div></article>`;
}

function workflowActions(workflow) {
  const stages = workflow.stages || [];
  const tester = stages.find((stage) => stage.stage === 'test');
  const reviewer = stages.find((stage) => stage.stage === 'review');
  const actions = [];
  const canOperate = ['operator','admin'].includes(sessionIdentity.role);
  const isAdmin = sessionIdentity.role === 'admin';
  if (canOperate && ['failed','blocked'].includes(workflow.status)) actions.push(['retry','Retry failed stage']);
  if (canOperate && workflow.reviewer_verdict === 'REJECT' && reviewer?.status === 'completed') actions.push(['rereview','Run review again']);
  if (isAdmin && workflow.reviewer_verdict === 'APPROVE' && tester?.status === 'completed') actions.push(['approve','Approve changes']);
  if (isAdmin && tester?.status === 'approved') actions.push(['merge','Merge into main']);
  if (isAdmin && tester?.status === 'merged') actions.push(['push','Push to GitHub']);
  if (isAdmin && tester?.status === 'pushed') actions.push(['cleanup','Clean up worktree']);
  if (!actions.length) return sessionIdentity.role === 'viewer' ? '<p class="permission-note">View-only access</p>' : '';
  return `<section class="workflow-actions" aria-label="Workflow actions"><div><h3>Workflow controls</h3><p>Each action is validated by the agent gateway and requires confirmation.</p></div><div class="action-buttons">${actions.map(([action,label])=>`<button type="button" class="action-button ${['merge','push'].includes(action)?'primary':''}" data-workflow-action="${action}">${label}</button>`).join('')}</div><p id="action-status" class="action-status" role="status" aria-live="polite"></p></section>`;
}

function renderWorkflow(workflow) {
  activeWorkflowId = workflow.id;
  $('workflow-title').textContent = workflow.objective || 'Workflow';
  const stages = workflow.stages || [];
  const evidence = workflow.tester_evidence ? `<section class="detail-section"><h3>Tester evidence</h3><pre class="report">${safe(workflow.tester_evidence)}</pre></section>` : '';
  const review = workflow.reviewer_verdict ? `<span class="verdict ${workflow.reviewer_verdict.toLowerCase()}">${safe(workflow.reviewer_verdict)}</span>` : '<span class="hint">Pending</span>';
  const repair = repairBadge(workflow.repair_state, workflow.repair_attempts, workflow.repair_max_attempts);
  const diff = workflow.diff ? `<section class="detail-section"><details><summary>Unmerged Git diff</summary><pre class="diff"><code>${safe(workflow.diff)}</code></pre></details></section>` : '<section class="detail-section empty-detail"><h3>Unmerged Git diff</h3><p>No diff is available for this workflow.</p></section>';
  workflowContent.innerHTML = `<section class="workflow-summary"><div><span>Project</span><strong>${safe(workflow.project||'Unknown')}</strong></div><div><span>Status</span><strong class="status ${safe(workflow.status)}">${safe(workflow.status||'Unknown')}</strong></div><div><span>Duration</span><strong>${duration(workflow.elapsed_seconds)}</strong></div><div><span>Review</span>${review}</div><div><span>Repair</span>${repair}</div></section>${workflowActions(workflow)}<section class="detail-section"><div class="section-title"><h3>Ordered stages</h3><span class="hint">${stages.length} stages</span></div>${stages.length?`<div class="timeline">${stages.map(stageCard).join('')}</div>`:'<p class="empty-detail">No stages are available for this workflow.</p>'}</section>${evidence}${diff}`;
}

async function openWorkflow(workflowId) {
  activeWorkflowId = workflowId;
  $('workflow-title').textContent = 'Loading workflow\u2026';
  workflowContent.innerHTML = '<div class="drawer-state"><span class="spinner" aria-hidden="true"></span><p>Loading workflow details\u2026</p></div>';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}`,{cache:'no-store'});
    if (_handle401(response)) throw new Error('Session expired');
    if (!response.ok) throw new Error(response.status===404?'Workflow details are no longer available.':`Workflow detail returned ${response.status}.`);
    renderWorkflow(await response.json());
  } catch (error) {
    $('workflow-title').textContent = 'Unable to load workflow';
    workflowContent.innerHTML = `<div class="drawer-state error-state" role="alert"><p>${safe(error.message)}</p><button type="button" data-retry-id="${safe(workflowId)}">Try again</button></div>`;
  }
}

async function runWorkflowAction(action) {
  if (!activeWorkflowId) return;
  const labels = {retry:'retry the failed stage',rereview:'run the reviewer again',approve:'approve these changes',merge:'merge these changes into main',push:'push main to GitHub',cleanup:'remove the completed worktree'};
  if (!window.confirm(`Confirm you want to ${labels[action] || action}?\n\nWorkflow: ${activeWorkflowId}`)) return;
  const status = $('action-status');
  const buttons = workflowContent.querySelectorAll('[data-workflow-action]');
  buttons.forEach((button)=>button.disabled=true);
  if (status) status.textContent = `Running ${action}\u2026`;
  try {
    const response = await fetch(`/api/workflows/${encodeURIComponent(activeWorkflowId)}/actions/${encodeURIComponent(action)}`,{method:'POST',headers:_csrfHeaders(),body:JSON.stringify({confirm:activeWorkflowId})});
    if (_handle401(response)) return;
    const result = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(result.detail || `Action returned ${response.status}.`);
    await openWorkflow(activeWorkflowId);
    await load();
  } catch (error) {
    buttons.forEach((button)=>button.disabled=false);
    if (status) status.textContent = error.message;
  }
}

async function loadAllowedProjects() {
  try {
    const response = await fetch('/api/workflows/allowed-projects', {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    const select = $('build-project');
    select.innerHTML = (data.projects || []).map((p) => `<option value="${safe(p)}">${safe(p)}</option>`).join('');
  } catch { /* non-critical */ }
}

function setBuildStatus(message, isError) {
  const el = $('build-status');
  el.textContent = message;
  el.className = 'build-status' + (isError ? ' build-status-error' : '');
}

const SPEC_FIELDS = ['spec-goal', 'spec-acceptance', 'spec-scope', 'spec-exclusions', 'spec-required-tests', 'spec-notes'];

function composeObjective() {
  const parts = [];
  const goal = $('spec-goal').value.trim();
  const acceptance = $('spec-acceptance').value.trim();
  const scope = $('spec-scope').value.trim();
  const exclusions = $('spec-exclusions').value.trim();
  const requiredTests = $('spec-required-tests').value.trim();
  const notes = $('spec-notes').value.trim();
  if (goal) parts.push(`Goal: ${goal}`);
  if (acceptance) parts.push(`Acceptance Criteria: ${acceptance}`);
  if (scope) parts.push(`Scope: ${scope}`);
  if (exclusions) parts.push(`Exclusions: ${exclusions}`);
  if (requiredTests) parts.push(`Required Tests: ${requiredTests}`);
  if (notes) parts.push(`Notes: ${notes}`);
  return parts.join(' | ');
}

function updatePreview() {
  const objective = composeObjective();
  $('build-preview-text').textContent = objective;
  const len = objective.length;
  $('build-char-count').textContent = `${len}/2000`;
  const overLimit = len > 2000;
  $('build-preview').classList.toggle('over-limit', overLimit);
  if (overLimit) {
    $('build-submit').disabled = true;
  } else if (sessionIdentity.role !== 'viewer') {
    $('build-submit').disabled = false;
  }
}

function setBuildDisabled(disabled) {
  $('build-submit').disabled = disabled;
  $('build-project').disabled = disabled;
  SPEC_FIELDS.forEach((id) => { $(id).disabled = disabled; });
  $('template-select').disabled = disabled;
  $('template-save').disabled = disabled;
  $('template-delete').disabled = disabled;
  document.querySelectorAll('#start-build-form input[type="radio"]').forEach((r) => { r.disabled = disabled; });
}

function normalizeBuildIntent(project, objective, reasoning) {
  return `${project}\u0000${objective}\u0000${reasoning}`;
}

function generateUUID() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    // Set version 4 bits (RFC 4122 §4.4)
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    // Set variant 10xx bits (RFC 4122 §4.3)
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  throw new Error('Web Crypto API is unavailable in this browser. Use a modern browser or serve over HTTPS.');
}

function getBuildKey(project, objective, reasoning) {
  const intent = normalizeBuildIntent(project, objective, reasoning);
  if (buildKey && buildKeyIntent === intent) return buildKey;
  const newKey = generateUUID();
  buildKey = newKey;
  buildKeyIntent = intent;
  return buildKey;
}

function rotateBuildKey(project, objective, reasoning) {
  const intent = normalizeBuildIntent(project, objective, reasoning);
  const newKey = generateUUID();
  buildKey = newKey;
  buildKeyIntent = intent;
}

function clearBuildKey() {
  buildKey = null;
  buildKeyIntent = null;
}

async function startBuild(event) {
  event.preventDefault();
  if (buildInFlight) return;
  const project = $('build-project').value;
  const objective = composeObjective();
  const reasoning = document.querySelector('input[name="reasoning"]:checked')?.value || 'standard';

  if (!project || !objective) {
    setBuildStatus('Please select a project and enter an objective.', true);
    return;
  }

  buildInFlight = true;
  setBuildDisabled(true);
  setBuildStatus('Submitting\u2026');

  try {
    const idempotencyKey = getBuildKey(project, objective, reasoning);
    const response = await fetch('/api/workflows', {
      method: 'POST',
      headers: _csrfHeaders(),
      body: JSON.stringify({project, objective, reasoning, idempotency_key: idempotencyKey}),
    });
    if (_handle401(response)) { setBuildDisabled(false); buildInFlight = false; return; }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 409) {
        rotateBuildKey(project, objective, reasoning);
        setBuildStatus('Duplicate submission detected. Please try again.', true);
      } else if (response.status === 403) {
        setBuildStatus(result.detail || 'You do not have permission to start builds.', true);
      } else if (response.status === 502) {
        setBuildStatus(result.detail || 'Gateway unavailable. Please try again.', true);
      } else {
        setBuildStatus(result.detail || `Submission failed (${response.status}).`, true);
      }
      setBuildDisabled(false);
      buildInFlight = false;
      return;
    }
    if (result.pending) {
      setBuildStatus(`Workflow ${result.id || ''} is processing. You can retry.`, false);
      setBuildDisabled(false);
      buildInFlight = false;
      return;
    }
    setBuildStatus(`Workflow ${result.id || ''} started (${result.status || 'queued'}).`);
    SPEC_FIELDS.forEach((id) => { $(id).value = ''; });
    updatePreview();
    clearBuildKey();
    setBuildDisabled(false);
    buildInFlight = false;
    await load();
    if (result.id) openWorkflow(result.id);
  } catch (error) {
    setBuildStatus(error.message || 'Network error. Please try again.', true);
    setBuildDisabled(false);
    buildInFlight = false;
  }
}

SPEC_FIELDS.forEach((id) => { $(id).addEventListener('input', updatePreview); });
$('start-build-form').addEventListener('submit', startBuild);

async function loadTemplates(project) {
  const select = $('template-select');
  select.innerHTML = '<option value="">— No template —</option>';
  $('template-delete').disabled = true;
  if (!project) return;
  try {
    const response = await fetch(`/api/templates?project=${encodeURIComponent(project)}`, {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    const templates = data.templates || [];
    templates.forEach((t) => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = t.name;
      select.appendChild(opt);
    });
  } catch { /* non-critical */ }
}

function applyTemplate(templateId) {
  if (!templateId) return;
  const select = $('template-select');
  const project = $('build-project').value;
  fetch(`/api/templates?project=${encodeURIComponent(project)}`, {cache: 'no-store'})
    .then((r) => r.ok ? r.json() : null)
    .then((data) => {
      if (!data) return;
      const t = (data.templates || []).find((x) => x.id === templateId);
      if (!t) return;
      $('spec-goal').value = t.spec.goal || '';
      $('spec-acceptance').value = t.spec.acceptance || '';
      $('spec-scope').value = t.spec.scope || '';
      $('spec-exclusions').value = t.spec.exclusions || '';
      $('spec-required-tests').value = t.spec.required_tests || '';
      $('spec-notes').value = t.spec.notes || '';
      $('template-delete').disabled = false;
      updatePreview();
    })
    .catch(() => {});
}

async function saveTemplate() {
  const project = $('build-project').value;
  if (!project) { setBuildStatus('Select a project first.', true); return; }
  const name = window.prompt('Template name:');
  if (!name || !name.trim()) return;
  const body = {
    project,
    name: name.trim().slice(0, 100),
    spec: {
      goal: $('spec-goal').value.trim(),
      acceptance: $('spec-acceptance').value.trim(),
      scope: $('spec-scope').value.trim(),
      exclusions: $('spec-exclusions').value.trim(),
      required_tests: $('spec-required-tests').value.trim(),
      notes: $('spec-notes').value.trim(),
    },
  };
  try {
    const response = await fetch('/api/templates', {method: 'POST', headers: _csrfHeaders(), body: JSON.stringify(body)});
    if (_handle401(response)) return;
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      setBuildStatus(result.detail || 'Failed to save template.', true);
      return;
    }
    setBuildStatus('Template saved.');
    await loadTemplates(project);
  } catch (error) {
    setBuildStatus(error.message || 'Failed to save template.', true);
  }
}

async function deleteTemplate() {
  const select = $('template-select');
  const id = select.value;
  if (!id) return;
  const project = $('build-project').value;
  if (!project) return;
  if (!window.confirm('Delete this template?')) return;
  try {
    const response = await fetch(`/api/templates/${encodeURIComponent(id)}?project=${encodeURIComponent(project)}`, {method: 'DELETE', headers: _csrfHeaders()});
    if (_handle401(response)) return;
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      setBuildStatus(result.detail || 'Failed to delete template.', true);
      return;
    }
    setBuildStatus('Template deleted.');
    await loadTemplates($('build-project').value);
  } catch (error) {
    setBuildStatus(error.message || 'Failed to delete template.', true);
  }
}

$('template-select').addEventListener('change', (e) => { applyTemplate(e.target.value); });
$('template-save').addEventListener('click', saveTemplate);
$('template-delete').addEventListener('click', deleteTemplate);
$('build-project').addEventListener('change', () => { loadTemplates($('build-project').value); });

async function loadClusterHealth() {
  try {
    const response = await fetch('/api/cluster-health', {cache: 'no-store'});
    if (_handle401(response)) return;
    if (!response.ok) throw new Error(`Health check returned ${response.status}`);
    renderClusterHealth(await response.json());
  } catch (error) {
    renderClusterHealthError();
  }
}

function renderClusterHealth(data) {
  const container = $('cluster-health');
  if (!container) return;
  const overall = data.overall || 'unknown';
  const timestamp = data.generated_at ? new Date(data.generated_at).toLocaleTimeString() : '';
  $('health-timestamp').textContent = timestamp ? `Checked ${timestamp}` : '';

  const services = Array.isArray(data.services) ? data.services : [];
  const lmStudio = data.lm_studio || {};
  const agentQueue = data.agent_queue || {};

  const overallBadge = `<div class="health-overall status ${safe(overall)}"><span>${safe(overall)}</span></div>`;

  const serviceCards = services.map((svc) => {
    const status = svc.status || 'unknown';
    const latency = svc.latency_ms != null ? `${svc.latency_ms}ms` : '\u2014';
    const detail = svc.detail ? `<small class="health-detail">${safe(svc.detail)}</small>` : '';
    return `<article class="health-card"><div class="health-card-head"><span class="status ${safe(status)}">${safe(status)}</span><span class="health-latency">${latency}</span></div><strong>${safe(svc.name)}</strong>${detail}</article>`;
  }).join('');

  const models = Array.isArray(lmStudio.models) ? lmStudio.models : [];
  const lmStatus = lmStudio.status || 'unknown';
  const lmSection = `<article class="health-card health-card-wide"><div class="health-card-head"><span class="status ${safe(lmStatus)}">${safe(lmStatus)}</span><span class="health-latency">LM Studio models</span></div>${models.length ? `<ul class="model-list" aria-label="Loaded models">${models.map((m) => `<li><span class="status ${m.loaded ? 'healthy' : 'unknown'}">${m.loaded ? 'loaded' : 'available'}</span><code>${safe(m.id)}</code></li>`).join('')}</ul>` : '<p class="muted-copy">No models reported.</p>'}</article>`;

  const queueSection = `<article class="health-card health-card-wide"><div class="health-card-head"><span class="status ${agentQueue.running > 0 ? 'running' : 'unknown'}">queue</span><span class="health-latency">${agentQueue.running || 0} running / ${agentQueue.queued || 0} queued</span></div>${agentQueue.current_job ? `<p class="health-current-job">Current: <code>${safe(agentQueue.current_job.id || '\u2014')}</code> \u00b7 ${safe(agentQueue.current_job.project || '\u2014')} \u00b7 ${safe(agentQueue.current_job.stage || '\u2014')}</p>` : '<p class="muted-copy">No active jobs.</p>'}</article>`;

  const current = agentQueue.current_job;
  const currentMetric = current && current.duplicate_tool_call_count != null
    ? `<span class="health-efficiency ${current.duplicate_warning ? 'health-efficiency-warning' : ''}">Duplicate tool calls: ${safe(current.duplicate_tool_call_count)}${current.duplicate_warning ? ' ⚠' : ''}</span>`
    : '<span class="health-efficiency">Duplicate tool calls: —</span>';
  const workflowEfficiency = Array.isArray(agentQueue.workflow_efficiency) ? agentQueue.workflow_efficiency : [];
  const efficiencyList = workflowEfficiency.length
    ? `<ul class="health-efficiency-list" aria-label="Recent workflow efficiency">${workflowEfficiency.map((workflow) => `<li><code>${safe(workflow.id || '—')}</code><span>${safe(workflow.project || '—')}</span><span>${workflow.total_tokens != null ? `${safe(workflow.total_tokens)} tokens` : '— tokens'}</span><span class="${workflow.duplicate_warning ? 'health-efficiency-warning' : ''}">${workflow.duplicate_tool_call_count != null ? `${safe(workflow.duplicate_tool_call_count)} duplicate calls${workflow.duplicate_warning ? ' ⚠' : ''}` : 'Duplicate calls: —'}</span></li>`).join('')}</ul>`
    : '<p class="muted-copy">No completed workflow efficiency data.</p>';
  const efficiencySection = `<article class="health-card health-card-wide"><div class="health-card-head"><span class="status ${current && current.duplicate_warning ? 'degraded' : 'healthy'}">efficiency</span><span class="health-latency">duplicate-call threshold ${safe(agentQueue.duplicate_tool_call_warning_threshold || '—')}</span></div>${current ? `<p class="health-current-job">Active job: <code>${safe(current.id || '—')}</code> · ${safe(current.project || '—')} · ${safe(current.stage || '—')}</p>${currentMetric}` : '<p class="muted-copy">No active jobs.</p>'}<div class="health-efficiency-summary"><strong>Recent workflows</strong>${efficiencyList}</div></article>`;
  container.innerHTML = `${overallBadge}<div class="health-cards">${serviceCards}${lmSection}${queueSection}${efficiencySection}</div>`;
}

function renderClusterHealthError() {
  const container = $('cluster-health');
  if (!container) return;
  $('health-timestamp').textContent = '';
  container.innerHTML = `<div class="health-overall status offline"><span>unavailable</span></div><p class="muted-copy">Health check could not be completed.</p>`;
}

async function load() {
  try { const response=await fetch('/api/dashboard',{cache:'no-store'}); if(_handle401(response)) throw new Error('Session expired'); if(!response.ok) throw new Error(`Dashboard returned ${response.status}`); render(await response.json()); }
  catch(error) { if(error.message==='Session expired') return; $('error').textContent=error.message; $('error').hidden=false; $('updated').textContent='Connection issue'; }
}

function _csrfHeaders() {
  const h = {'Content-Type': 'application/json'};
  if (sessionIdentity.csrf_token) h['X-CSRF-Token'] = sessionIdentity.csrf_token;
  return h;
}

function _handle401(response) {
  if (response.status === 401) {
    window.location.href = '/login';
    return true;
  }
  return false;
}

async function loadSession() {
  const response = await fetch('/api/session',{cache:'no-store'});
  if (_handle401(response)) throw new Error('Session expired');
  if (!response.ok) throw new Error(`Session returned ${response.status}`);
  sessionIdentity = await response.json();
  $('identity').textContent = `${sessionIdentity.username} \u00b7 ${sessionIdentity.role}`;
  if (sessionIdentity.role === 'viewer') {
    setBuildDisabled(true);
    $('build-permission-hint').textContent = 'Operator or admin required';
    const note = document.createElement('p');
    note.className = 'permission-note';
    note.textContent = 'Build actions require operator or admin access.';
    const form = $('start-build-form');
    if (form && !form.querySelector('.permission-note')) form.appendChild(note);
  } else {
    setBuildDisabled(false);
    $('build-permission-hint').textContent = '';
  }
}

async function logout() {
  try {
    await fetch('/api/logout', {method: 'POST'});
  } catch { /* ignore */ }
  window.location.href = '/login';
}

$('logout').addEventListener('click', logout);

$('jobs').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
$('workflows').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
workflowContent.addEventListener('click',(event)=>{const retry=event.target.closest('[data-retry-id]');if(retry)openWorkflow(retry.dataset.retryId);const action=event.target.closest('[data-workflow-action]');if(action)runWorkflowAction(action.dataset.workflowAction);});
$('workflow-close').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',(event)=>{if(event.target===dialog)dialog.close();});
async function loadAlerts() {
  try {
    const response = await fetch('/api/alerts', {cache: 'no-store'});
    if (_handle401(response)) return;
    if (!response.ok) throw new Error(`Alerts returned ${response.status}`);
    renderAlerts(await response.json());
  } catch (error) {
    const container = $('alerts');
    if (container) container.innerHTML = '<p class="muted-copy">Alert data unavailable.</p>';
  }
}

function renderAlerts(data) {
  const container = $('alerts');
  const historyContainer = $('alerts-history');
  const hint = $('alerts-hint');
  if (!container) return;
  const active = Array.isArray(data.active) ? data.active : [];
  const history = Array.isArray(data.history) ? data.history : [];
  if (hint) hint.textContent = active.length ? `${active.length} active` : 'None active';
  const isAdmin = sessionIdentity.role === 'admin';
  container.innerHTML = active.length ? active.map((a) => {
    const id = a.job_id ? safe(a.job_id) : '';
    const project = a.project ? safe(a.project) : '\u2014';
    const status = a.status ? safe(a.status) : 'unknown';
    const created = a.created_at ? new Date(a.created_at).toLocaleString() : '\u2014';
    const btn = isAdmin ? `<button type="button" class="acknowledge-btn" data-ack-job-id="${id}" aria-label="Acknowledge alert ${id}">Acknowledge</button><span class="alert-status" role="status" aria-live="polite" data-ack-status="${id}"></span>` : '';
    return `<div class="alert-item"><div class="alert-item-head"><code>${id}</code><span class="status ${status}">${status}</span></div><small>${project} \u00b7 ${created}</small>${btn ? `<div class="alert-actions">${btn}</div>` : ''}</div>`;
  }).join('') : '<p class="muted-copy">No active alerts.</p>';
  if (historyContainer) {
    historyContainer.innerHTML = history.length ? `<h3 class="alerts-history-title">Recently acknowledged</h3><div class="alerts-history-list">${history.map((a) => {
      const id = a.job_id ? safe(a.job_id) : '';
      const project = a.project ? safe(a.project) : '\u2014';
      const ackedAt = a.acknowledged_at ? new Date(a.acknowledged_at).toLocaleString() : '\u2014';
      return `<div class="alert-item alert-acked"><code>${id}</code><small>${project} \u00b7 ${ackedAt}</small></div>`;
    }).join('')}</div>` : '';
  }
}

let ackInFlight = false;

async function acknowledgeAlert(jobId) {
  if (ackInFlight) return;
  const submitBtn = document.querySelector(`[data-ack-job-id="${jobId}"]`);
  const status = document.querySelector(`[data-ack-status="${jobId}"]`);

  ackInFlight = true;
  if (submitBtn) submitBtn.disabled = true;
  if (status) status.textContent = 'Acknowledging\u2026';

  try {
    const response = await fetch(`/api/alerts/${encodeURIComponent(jobId)}/acknowledge`, {
      method: 'POST',
      headers: _csrfHeaders(),
      body: JSON.stringify({resolution_note: 'Acknowledged from dashboard', confirm: jobId}),
    });
    if (_handle401(response)) { if (status) status.textContent = 'Session expired. Please sign in again.'; return; }
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `Acknowledgment returned ${response.status}.`);
    if (status) status.textContent = 'Acknowledged.';
    await loadAlerts();
    await load();
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    ackInFlight = false;
    if (submitBtn) submitBtn.disabled = false;
  }
}

document.addEventListener('click', (event) => {
  const trigger = event.target.closest('[data-ack-job-id]');
  if (trigger) acknowledgeAlert(trigger.dataset.ackJobId);
});

$('refresh').addEventListener('click', () => { load(); loadAlerts(); });
async function initialize(){try{await loadSession();}catch(error){$('identity').textContent='Access unavailable';}await loadAllowedProjects();if(sessionIdentity.role!=='viewer'){await loadTemplates($('build-project').value);}await load();await loadClusterHealth();await loadAlerts();setInterval(load,15000);setInterval(loadClusterHealth,30000);setInterval(loadAlerts,30000);}
initialize();
