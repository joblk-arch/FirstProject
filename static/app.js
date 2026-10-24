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

function renderSummaries(data) {
  const wfHint = $('recent-workflows-hint');
  const aaHint = $('recent-agent-activity-hint');
  const wdHint = $('work-distribution-hint');
  const tlHint = $('token-ledger-hint');
  if (wfHint) {
    if (!data || !Array.isArray(data.recent_workflows)) {
      wfHint.textContent = 'Unavailable';
    } else {
      let valid = true;
      let running = 0, attention = 0;
      for (const w of data.recent_workflows) {
        if (w === null || typeof w !== 'object' || Array.isArray(w) || typeof w.status !== 'string') { valid = false; break; }
        if (w.status === 'running') running++;
        else if (w.status === 'failed' || w.status === 'blocked') attention++;
      }
      wfHint.textContent = valid ? `Recent sample: ${running} running \u00b7 ${attention} need attention` : 'Unavailable';
    }
  }
  if (aaHint) {
    if (!data || !Array.isArray(data.counts)) {
      aaHint.textContent = 'Unavailable';
    } else {
      let valid = true;
      let running = 0, queued = 0;
      for (const entry of data.counts) {
        if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) { valid = false; break; }
        if (typeof entry.status !== 'string') { valid = false; break; }
        if (typeof entry.count !== 'number' || typeof entry.count === 'boolean' || !Number.isSafeInteger(entry.count) || entry.count < 0) { valid = false; break; }
        if (entry.status === 'running') running += entry.count;
        else if (entry.status === 'queued') queued += entry.count;
      }
      if (valid && (!Number.isSafeInteger(running) || !Number.isSafeInteger(queued))) valid = false;
      aaHint.textContent = valid ? `${running} running \u00b7 ${queued} queued` : 'Unavailable';
    }
  }
  if (wdHint) {
    if (!data || !Array.isArray(data.projects)) {
      wdHint.textContent = 'Unavailable';
    } else {
      let valid = true;
      for (const p of data.projects) {
        if (p === null || typeof p !== 'object' || Array.isArray(p) || typeof p.name !== 'string') { valid = false; break; }
      }
      wdHint.textContent = valid ? `${data.projects.length} projects` : 'Unavailable';
    }
  }
  if (tlHint) {
    if (!data || !Array.isArray(data.usage)) {
      tlHint.textContent = 'Unavailable';
    } else {
      let valid = true;
      let total = 0;
      for (const item of data.usage) {
        if (item === null || typeof item !== 'object' || Array.isArray(item)) { valid = false; break; }
        if (typeof item.total_tokens !== 'number' || typeof item.total_tokens === 'boolean' || !Number.isSafeInteger(item.total_tokens) || item.total_tokens < 0) { valid = false; break; }
        total += item.total_tokens;
      }
      if (valid && !Number.isSafeInteger(total)) valid = false;
      tlHint.textContent = valid ? `${fmt(total)} recorded tokens` : 'Unavailable';
    }
  }
}

function render(data) {
  if (typeof renderIsolatedWorkflows === 'function') renderIsolatedWorkflows(data.isolated_workflows);
  renderSummaries(data);
  renderRecentWorkflowReliability(data.recent_workflow_reliability);
  renderWorkflowBottlenecks(data.workflow_bottlenecks);
  renderCompletionRecovery(data.completion_recovery_summary);
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
  // status_counts card is rendered in renderClusterHealth (it comes from /api/cluster-health)
  $('jobs').innerHTML = jobs.length ? jobs.slice(0,20).map((job) => {
    const label = safe(job.stage || job.role);
    const stage = job.workflow_id ? `<button class="workflow-link" type="button" data-workflow-id="${safe(job.workflow_id)}" aria-label="Open ${label} workflow details">${label}</button>` : label;
    return `<tr><td>${stage}</td><td>${safe(job.project)}</td><td><span class="status ${safe(job.status)}">${safe(job.status)}</span></td><td>${modelWithReason(job.model, job.model_reason)}</td><td>${job.total_tokens == null ? '\u2014' : fmt(job.total_tokens)}</td><td>${duration(job.duration_seconds)}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">No agent jobs yet.</td></tr>';
  renderRunningNow(jobs);
  renderAgentActivity(counts);
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
  renderValidationDiagnostics(data.jobs);
}

function renderUpsStatus(data) {
  const grid = $('ups-status');
  const ts = $('ups-timestamp');
  if (!grid) return;
  const POLICY_TEXT = 'Shutdown below 40% on UPS power only';
  function fmtTime(v) {
    if (!v) return '\u2014';
    const d = new Date(v);
    return isNaN(d.getTime()) ? '\u2014' : d.toLocaleString();
  }
  function healthClass(h, overall) {
    if (overall !== 'healthy') return 'unavailable';
    if (h === 'fresh') return 'healthy';
    if (h === 'stale') return 'stale';
    return 'unavailable';
  }
  function healthLabel(h, overall) {
    if (overall !== 'healthy') return 'Unavailable';
    if (h === 'fresh') return 'Available';
    if (h === 'stale') return 'Stale';
    return 'Unavailable';
  }
  // Determine overall status
  let status = 'unavailable';
  let statusLabel = 'Unavailable';
  if (data) {
    const validSH = ['fresh', 'stale', 'degraded', 'unavailable'];
    const sh = data.source_health;
    if (typeof sh !== 'string' || !validSH.includes(sh)) {
      status = 'unavailable'; statusLabel = 'Unavailable';
    } else {
      const monitorRunning = data.monitor_state === 'running';
      const present = data.ups_present === true;
      const ps = data.power_source;
      const powerValid = (ps === 'ac' || ps === 'ups');
      const bp = data.battery_percent;
      const validBattery = Number.isInteger(bp) && bp >= 0 && bp <= 100;
      const sf = data.source_fresh;
      const st = data.stale;
      const contradictory = (sf === true && (st === true || sh !== 'fresh'));
      if (contradictory) {
        status = 'degraded'; statusLabel = 'Degraded (contradictory flags)';
      } else if (sh === 'stale') {
        status = 'stale'; statusLabel = 'Stale';
      } else if (st === true && monitorRunning && present && powerValid && validBattery) {
        status = 'stale'; statusLabel = 'Stale';
      } else if (sh === 'fresh' && sf === true && st !== true) {
        if (!monitorRunning) { status = 'degraded'; statusLabel = 'Degraded (monitor stopped)'; }
        else if (!present) { status = 'degraded'; statusLabel = 'No UPS detected'; }
        else if (!validBattery) { status = 'degraded'; statusLabel = 'Degraded (invalid battery)'; }
        else if (!powerValid) { status = 'degraded'; statusLabel = 'Unknown power source'; }
        else if (ps === 'ac') { status = 'healthy'; statusLabel = 'AC Power'; }
        else { status = 'warning'; statusLabel = 'On UPS power'; }
      } else if (sh === 'degraded') {
        status = 'degraded'; statusLabel = 'Degraded';
      } else if (sh === 'unavailable') {
        status = 'unavailable'; statusLabel = 'Unavailable';
      } else {
        status = 'degraded'; statusLabel = 'Degraded';
      }
    }
  }
  // Validate fields defensively
  const powerSource = (data && (data.power_source === 'ac' || data.power_source === 'ups')) ? data.power_source : 'unknown';
  const battery = (data && typeof data.battery_percent === 'number' && !Number.isNaN(data.battery_percent) && data.battery_percent >= 0 && data.battery_percent <= 100) ? data.battery_percent : null;
  const upsPresent = (data && typeof data.ups_present === 'boolean') ? data.ups_present : null;
  const monitorState = (data && (data.monitor_state === 'running' || data.monitor_state === 'stopped')) ? data.monitor_state : 'unknown';
  const lastUpdated = (data && data.last_updated) ? data.last_updated : null;
  const lastEvent = (data && typeof data.last_event === 'string') ? data.last_event.slice(0, 500) : '';
  const sourceHealth = (data && typeof data.source_health === 'string') ? data.source_health : 'unavailable';
  const hClass = healthClass(sourceHealth, status);
  const hLabel = healthLabel(sourceHealth, status);
  // Build HTML with safe() escaping for all API strings
  const psDisplay = powerSource === 'unknown' ? 'Unknown' : powerSource.toUpperCase();
  const batDisplay = battery !== null ? battery + '%' : '\u2014';
  const presDisplay = upsPresent === null ? 'Unknown' : (upsPresent ? 'Yes' : 'No');
  const monDisplay = monitorState === 'unknown' ? 'Unknown' : monitorState;
  const luDisplay = fmtTime(lastUpdated);
  const evDisplay = lastEvent ? safe(lastEvent) : '';
  const evRow = lastEvent ? `<div class="ups-field ups-event"><span class="ups-label">Last event</span><span class="ups-value">${evDisplay}</span></div>` : '';
  const upsHint = $('ups-summary-hint');
  if (upsHint) {
    if (status === 'healthy' && battery !== null) {
      upsHint.textContent = `Utility power · Battery ${battery}%`;
    } else if (status === 'warning' && battery !== null) {
      upsHint.textContent = `On battery · Battery ${battery}%`;
    } else {
      upsHint.textContent = statusLabel;
    }
  }
  grid.innerHTML =
    `<div class="ups-badge ${status}">${safe(statusLabel)}</div>` +
    `<div class="ups-source ${hClass}">Source: ${safe(hLabel)}</div>` +
    `<div class="ups-field"><span class="ups-label">Power source</span><span class="ups-value">${safe(psDisplay)}</span></div>` +
    `<div class="ups-field"><span class="ups-label">Battery</span><span class="ups-value">${safe(batDisplay)}</span></div>` +
    `<div class="ups-field"><span class="ups-label">UPS present</span><span class="ups-value">${safe(presDisplay)}</span></div>` +
    `<div class="ups-field"><span class="ups-label">Monitor</span><span class="ups-value">${safe(monDisplay)}</span></div>` +
    `<div class="ups-field"><span class="ups-label">Last updated</span><span class="ups-value">${safe(luDisplay)}</span></div>` +
    evRow +
    `<div class="ups-policy">${safe(POLICY_TEXT)}</div>`;
  if (ts) ts.textContent = lastUpdated ? 'Updated ' + fmtTime(lastUpdated) : 'No data';
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
    if (response.status === 401) { renderClusterHealthError(); _handle401(response); return; }
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

  const healthHint = $('system-health-hint');
  if (healthHint) {
    const problem = value => ['degraded', 'offline', 'error'].includes(value);
    const services = Array.isArray(data.services) ? data.services : [];
    const statuses = services.map(svc => svc && typeof svc === 'object' && !Array.isArray(svc) ? svc.status : null);
    const lmStatus = data.lm_studio && !Array.isArray(data.lm_studio) ? data.lm_studio.status : null;
    const supported = statuses.length > 0 && statuses.every(value => value === 'healthy') && lmStatus === 'healthy';
    healthHint.textContent = problem(overall) || statuses.some(problem) || problem(lmStatus)
      ? 'Needs attention' : overall === 'healthy' && supported ? 'Healthy' : 'Unavailable';
  }

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
  const continuations = Array.isArray(agentQueue.needs_continuation) ? agentQueue.needs_continuation : [];
  const continuationSection = continuations.length ? `<article class="health-card health-card-wide"><div class="health-card-head"><span class="status degraded">needs continuation</span><span class="health-latency">${safe(continuations.length)} job(s)</span></div><ul class="health-efficiency-list" aria-label="Jobs needing continuation">${continuations.map((job) => `<li><code>${safe(job.id || '—')}</code><span>${safe(job.project || '—')} · ${safe(job.stage || '—')}</span><span>${safe(job.steps_used == null ? '—' : job.steps_used)} steps</span><span>${job.source_mutated ? 'source changed' : 'no source change'} · ${job.test_ran ? 'tests ran' : 'tests not run'}</span>${job.workflow_id ? `<button type="button" data-workflow-id="${safe(job.workflow_id)}">Review and retry</button>` : ''}</li>`).join('')}</ul></article>` : '';
  container.innerHTML = `${overallBadge}<div class="health-cards">${serviceCards}${lmSection}${queueSection}${efficiencySection}${continuationSection}</div>`;
  renderStatusCountsCard(agentQueue.status_counts, data.generated_at || null);
}

function renderStatusCountsCard(statusCounts, generatedAt) {
  const card = $('reliability-card');
  if (!card) return;
  const _SAFE_BOUND = 10000000;
  const safeCount = (v) => {
    if (v === null || v === undefined) return 'Unavailable';
    if (typeof v !== 'number' || !Number.isInteger(v) || v < 0 || v > _SAFE_BOUND) return 'Unavailable';
    return String(v);
  };
  const sc = statusCounts || {};
  let timestampHtml;
  if (generatedAt) {
    const d = new Date(generatedAt);
    if (!isNaN(d.getTime())) {
      timestampHtml = `<span class="reliability-timestamp">Last checked: ${safe(d.toLocaleString())}</span>`;
    } else {
      timestampHtml = `<span class="reliability-timestamp">Last checked: Unavailable</span>`;
    }
  } else {
    timestampHtml = `<span class="reliability-timestamp">Last checked: Unavailable</span>`;
  }
  card.innerHTML = `<h2>All-time job status counts</h2><p class="reliability-note">Completed does not mean merged; totals include historical canaries.</p><div class="reliability-grid">${[['Completed',sc.completed],['Failed',sc.failed],['Blocked',sc.blocked]].map(([label,val]) => `<article class="metric"><span>${label}</span><strong>${safeCount(val)}</strong></article>`).join('')}</div>${timestampHtml}`;
}

function renderClusterHealthError() {
  const container = $('cluster-health');
  if (!container) return;
  $('health-timestamp').textContent = '';
  const healthHint = $('system-health-hint');
  if (healthHint) healthHint.textContent = 'Unavailable';
  container.innerHTML = `<div class="health-overall status offline"><span>unavailable</span></div><p class="muted-copy">Health check could not be completed.</p>`;
  renderStatusCountsCard(null, null);
}

function renderRunningNow(jobs) {
  const container = $('running-now');
  if (!container) return;
  const running = (jobs || []).filter((j) => j.status === 'running');
  if (!running.length) {
    container.innerHTML = '<p class="muted-copy">No jobs running</p>';
    return;
  }
  container.innerHTML = running.map((job) => {
    const project = safe(job.project || '\u2014');
    const stage = safe(job.stage || job.role || '\u2014');
    const elapsed = job.duration_seconds != null ? `${duration(job.duration_seconds)} reported elapsed time` : 'Timing unavailable';
    return `<div class="running-item"><strong>${project}</strong><span>${stage}</span><small>${elapsed}</small></div>`;
  }).join('');
}

function renderAgentActivity(counts) {
  const root = $('agent-activity-body');
  if (!root) return;
  const unavailable = () => {
    root.innerHTML = `<div class="health-card"><div class="health-card-head"><span class="status">Unavailable</span><span class="health-latency">Counts unavailable</span></div><p class="muted-copy">Occupied slots reflect running jobs; total worker capacity is not reported.</p></div>`;
  };
  if (counts === null || counts === undefined) { unavailable(); return; }
  if (!Array.isArray(counts)) { unavailable(); return; }
  for (const entry of counts) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) { unavailable(); return; }
    if (typeof entry.status !== 'string') { unavailable(); return; }
    if (typeof entry.count !== 'number' || !Number.isSafeInteger(entry.count) || entry.count < 0) { unavailable(); return; }
  }
  let running = 0, queued = 0;
  for (const entry of counts) {
    if (entry.status === 'running') running += entry.count;
    else if (entry.status === 'queued') queued += entry.count;
  }
  if (!Number.isSafeInteger(running) || !Number.isSafeInteger(queued)) { unavailable(); return; }
  const occupied = running;
  root.innerHTML = `<div class="health-card"><div class="health-card-head"><span class="status running">${safe(String(running))}</span><span class="health-latency">Running agent jobs</span></div><div class="health-card-head"><span class="status queued">${safe(String(queued))}</span><span class="health-latency">Queued jobs</span></div><div class="health-card-head"><span class="status running">${safe(String(occupied))}</span><span class="health-latency">Occupied worker slots</span></div><p class="muted-copy">Occupied slots reflect running jobs; total worker capacity is not reported.</p></div>`;
}

async function loadUpsStatus() {
  try {
    const response = await fetch('/api/ups-status', { cache: 'no-store' });
    if (response.status === 401) { renderUpsStatus(null); _handle401(response); return; }
    if (!response.ok) { renderUpsStatus(null); return; }
    const data = await response.json();
    renderUpsStatus(data);
  } catch (error) {
    renderUpsStatus(null);
  }
}

async function load() {
  try { const response=await fetch('/api/dashboard',{cache:'no-store'}); if(_handle401(response)) throw new Error('Session expired'); if(!response.ok) throw new Error(`Dashboard returned ${response.status}`); render(await response.json()); }
  catch(error) { window.renderIsolatedWorkflows?.(null); renderSummaries(null); renderRecentWorkflowReliability(null); renderWorkflowBottlenecks(null); renderCompletionRecovery(null); renderValidationDiagnostics(null); renderAgentActivity(null); if(error.message==='Session expired') return; $('error').textContent=error.message; $('error').hidden=false; $('updated').textContent='Connection issue'; }
  loadUpsStatus();
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
  if (sessionIdentity.role === 'admin' && typeof window.initUserManagement === 'function') {
    window.initUserManagement(sessionIdentity);
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
$('cluster-health').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
$('validation-diagnostics').addEventListener('click', handleValidationWorkflowClick);
workflowContent.addEventListener('click',(event)=>{const retry=event.target.closest('[data-retry-id]');if(retry)openWorkflow(retry.dataset.retryId);const action=event.target.closest('[data-workflow-action]');if(action)runWorkflowAction(action.dataset.workflowAction);});
$('workflow-close').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',(event)=>{if(event.target===dialog)dialog.close();});
async function loadAlerts() {
  const hint = $('alerts-hint');
  try {
    const response = await fetch('/api/alerts', {cache: 'no-store'});
    if (response.status === 401) { if (hint) hint.textContent = 'Alert data unavailable'; _handle401(response); return; }
    if (!response.ok) throw new Error(`Alerts returned ${response.status}`);
    renderAlerts(await response.json());
  } catch (error) {
    if (hint) hint.textContent = 'Alert data unavailable';
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
  if (hint) hint.textContent = active.length ? `${active.length} awaiting acknowledgement` : 'None active';
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

function renderRecentWorkflowReliability(data) {
  const card = $('recent-workflow-reliability');
  if (!card) return;
  const si = (v) => (typeof v === 'number' && Number.isSafeInteger(v) && v >= 0) ? v : null;
  const sd = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= Number.MAX_SAFE_INTEGER) ? v : null;
  const relHint = $('reliability-summary-hint');
  const setRelHint = (txt) => { if (relHint) relHint.textContent = txt; };
  const unavail = () => { setRelHint('Unavailable'); card.innerHTML = '<h2>Recent workflow reliability</h2><p class="reliability-note">Unavailable</p>'; };
  if (!data || typeof data !== 'object') { unavail(); return; }
  const sample = si(data.sample_count);
  if (sample === null || sample > 20) { unavail(); return; }
  const s = si(data.successful);
  const f = si(data.failed);
  const a = si(data.active);
  const x = si(data.excluded);
  if (s === null || f === null || a === null || x === null || s + f + a + x !== sample) {
    unavail();
    return;
  }
  if (sample === 0) {
    setRelHint('No recent workflows');
    card.innerHTML = '<h2>Recent workflow reliability</h2><p class="reliability-note">No recent workflows</p>';
    return;
  }
  setRelHint(`Recent sample: ${f} failed · ${a} active`);
  const denom = s + f;
  const rate = denom === 0 ? 'Unavailable' : (s / denom * 100).toFixed(1) + '%';
  const KNOWN_STAGES = ['plan', 'implement', 'test', 'review', 'reason', 'unknown'];
  const stageHtml = (counts, label) => {
    if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return `<p>${label}: Unavailable</p>`;
    const entries = KNOWN_STAGES.filter((k) => Object.prototype.hasOwnProperty.call(counts, k));
    if (entries.some((k) => si(counts[k]) === null)) return `<p>${label}: Unavailable</p>`;
    if (!entries.length) return `<p>${label}: None</p>`;
    return `<p>${label}: ${entries.map((k) => `${safe(k)}: ${counts[k]}`).join(' · ')}</p>`;
  };
  const dur = data.duration;
  const med = sd(dur && dur.median_seconds);
  const samples = si(dur && dur.sample_count);
  const durStr = med !== null && samples !== null && samples > 0 && samples <= s + f
    ? `${med.toFixed(1)}s (${samples} samples)` : 'Not recorded';
  const timingString = (stats) => {
    if (!stats || typeof stats !== 'object') return 'Not recorded';
    const m = sd(stats.median_seconds);
    const a = sd(stats.average_seconds);
    const sc = si(stats.sample_count);
    if (m === null && a === null) return 'Not recorded';
    if (sc === null || sc <= 0 || sc > s + f) return 'Not recorded';
    const parts = [];
    if (m !== null) parts.push(`median ${m.toFixed(1)}s`);
    if (a !== null) parts.push(`avg ${a.toFixed(1)}s`);
    parts.push(`${sc} samples`);
    return parts.join(', ');
  };
  const jobExecStr = timingString(data.job_execution_duration);
  const gapStr = timingString(data.between_job_gaps);
  const to = si(data.timing_observed_jobs);
  const tmi2 = si(data.timing_missing_jobs);
  let timingCoverage;
  if (to === null || tmi2 === null) {
    timingCoverage = 'Unavailable';
  } else {
    timingCoverage = `${to} valid job intervals / ${tmi2} missing or invalid intervals` + (tmi2 > 0 ? ' — Incomplete' : '');
  }
  const tok = (data.tokens && typeof data.tokens === 'object') ? data.tokens : null;
  let tokStr;
  if (!tok) {
    tokStr = 'Not recorded';
  } else {
    const tm = si(tok.measured_jobs);
    const tmi = si(tok.missing_jobs);
    const total = si(tok.total);
    if (tm === null || tmi === null) {
      tokStr = 'Not recorded';
    } else {
      tokStr = `${tm > 0 && total !== null ? String(total) : 'Not recorded'} (${tm} measured / ${tmi} missing jobs)` + (tmi > 0 ? ' — Incomplete' : '');
    }
  }
  const metrics = [['Approved / merged / pushed', s], ['Failed outcomes', f], ['Active', a], ['Excluded', x]]
    .map(([l, v]) => `<article class="metric"><span>${l}</span><strong>${v}</strong></article>`)
    .join('');
  const avg = sd(dur && dur.average_seconds);
  const avgStr = avg !== null ? `, avg ${avg.toFixed(1)}s` : '';
  card.innerHTML = `<h2>Recent workflow reliability</h2>` +
    `<p class="reliability-note">Latest ${sample} workflows (up to 20) · Success rate: ${rate} (${denom} assessed outcomes) · (End-to-end elapsed) Median completion: ${durStr}${avgStr} · Recorded tokens: ${tokStr}</p>` +
    `<p class="reliability-note">Job execution time: ${jobExecStr} · Between-job gaps: ${gapStr} · Timing coverage: ${timingCoverage}</p>` +
    `<div class="reliability-grid">${metrics}</div>` +
    stageHtml(data.failure_stage_counts, 'Failed stages') + stageHtml(data.blocked_stage_counts, 'Blocked stages') +
    '<p class="reliability-note">Active and excluded workflows are not in the success-rate denominator. Historical canaries are included.</p>' +
    '<p class="reliability-note">Execution time is recorded job wall time, not GPU-only compute. Gaps exclude initial queue wait; timestamps do not identify cause.</p>';
}

function renderWorkflowBottlenecks(data) {
  const card = $('workflow-bottlenecks');
  if (!card) return;
  const si = (v) => (typeof v === 'number' && Number.isSafeInteger(v) && v >= 0) ? v : null;
  const sd = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= Number.MAX_SAFE_INTEGER) ? v : null;
  const unavail = () => { card.innerHTML = '<h2>Workflow bottlenecks</h2><p class="reliability-note">Unavailable</p>'; };
  if (!data || typeof data !== 'object' || Array.isArray(data)) { unavail(); return; }
  const sample = si(data.sample_count);
  if (sample === null || sample > 20) { unavail(); return; }
  const KNOWN_STAGES = ['plan', 'implement', 'test', 'review', 'reason', 'unknown'];
  if (!Array.isArray(data.stages) || data.stages.length !== 6) { unavail(); return; }
  const seen = new Set();
  const rows = [];
  for (let i = 0; i < 6; i++) {
    const st = data.stages[i];
    if (!st || typeof st !== 'object' || Array.isArray(st)) { unavail(); return; }
    if (st.stage !== KNOWN_STAGES[i]) { unavail(); return; }
    if (seen.has(st.stage)) { unavail(); return; }
    seen.add(st.stage);
    const jc = si(st.job_count);
    if (jc === null) { unavail(); return; }
    const to = si(st.timing_observed_jobs);
    if (to === null) { unavail(); return; }
    const tmi = si(st.timing_missing_jobs);
    if (tmi === null) { unavail(); return; }
    if (to + tmi > jc) { unavail(); return; }
    const km = si(st.token_measured_jobs);
    if (km === null) { unavail(); return; }
    const kmi = si(st.token_missing_jobs);
    if (kmi === null) { unavail(); return; }
    if (km + kmi !== jc) { unavail(); return; }
    let ds;
    if (to === 0) {
      if (st.duration_seconds !== null) { unavail(); return; }
      ds = null;
    } else {
      ds = sd(st.duration_seconds);
      if (ds === null) { unavail(); return; }
    }
    let tt;
    if (km === 0) {
      if (st.total_tokens !== null) { unavail(); return; }
      tt = null;
    } else {
      tt = si(st.total_tokens);
      if (tt === null) { unavail(); return; }
    }
    rows.push({ stage: st.stage, jc, ds, to, tmi, tt, km, kmi });
  }
  if (sample === 0) {
    if (rows.some((r) => r.jc !== 0)) { unavail(); return; }
    card.innerHTML = '<h2>Workflow bottlenecks</h2><p class="reliability-note">No recent workflows</p>';
    return;
  }
  let highestTime = null, highestToken = null;
  let maxTime = 0, maxToken = 0;
  for (const r of rows) {
    if (r.ds !== null && r.ds > 0 && r.ds > maxTime) { maxTime = r.ds; highestTime = r.stage; }
    if (r.tt !== null && r.tt > 0 && r.tt > maxToken) { maxToken = r.tt; highestToken = r.stage; }
  }
  const body = rows.map((r) => {
    const timingIncomplete = r.tmi > 0;
    const tokensIncomplete = r.kmi > 0;
    const timeStr = r.ds === null ? 'Unavailable' : (r.ds > 0 ? r.ds.toFixed(1) + 's' : '0');
    const tokenStr = r.tt === null ? 'Unavailable' : (r.tt > 0 ? String(r.tt) : '0');
    const timingCov = `${r.to} measured / ${r.tmi} missing` + (timingIncomplete ? ' \u2014 Incomplete' : '');
    const tokenCov = `${r.km} measured / ${r.kmi} missing` + (tokensIncomplete ? ' \u2014 Incomplete' : '');
    return `<tr><td>${safe(r.stage)}</td><td>${r.jc}</td><td>${timeStr}</td><td>${tokenStr}</td><td>${timingCov}</td><td>${tokenCov}</td></tr>`;
  }).join('');
  const timeWinner = highestTime ? safe(highestTime) : 'None';
  const tokenWinner = highestToken ? safe(highestToken) : 'None';
  card.innerHTML = `<h2>Workflow bottlenecks</h2>` +
    `<p class="reliability-note">Latest ${sample} workflows (up to 20) \u00b7 Highest time: ${timeWinner} \u00b7 Highest tokens: ${tokenWinner}</p>` +
    `<table class="reliability-table"><thead><tr><th>Stage</th><th>Jobs</th><th>Recorded time</th><th>Recorded tokens</th><th>Timing coverage</th><th>Token coverage</th></tr></thead><tbody>${body}</tbody></table>` +
    '<p class="reliability-note">Summed recorded job wall time includes attempts/overlaps, excludes between-job waits, not GPU-only. Incomplete rankings may change.</p>';
}

function renderCompletionRecovery(data) {
  const el = $('completion-recovery');
  if (!el) return;
  const heading = '<h2>Completion recovery</h2>';
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  const keys = ['sample_count','implementer_jobs','observed_jobs','unknown_jobs','attempted_jobs','recovered_jobs','failed_jobs','not_attempted_jobs'];
  const counts = {};
  for (const k of keys) {
    const v = data[k];
    if (typeof v !== 'number' || v < 0 || !Number.isSafeInteger(v)) {
      el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
      return;
    }
    counts[k] = v;
  }
  const {sample_count, implementer_jobs, observed_jobs, unknown_jobs, attempted_jobs, recovered_jobs, failed_jobs, not_attempted_jobs} = counts;
  // Invariants
  if (observed_jobs + unknown_jobs !== implementer_jobs) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (attempted_jobs !== recovered_jobs + failed_jobs) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (observed_jobs !== not_attempted_jobs + attempted_jobs) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (sample_count > 20) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (sample_count === 0) {
    if (implementer_jobs !== 0 || observed_jobs !== 0 || unknown_jobs !== 0 || attempted_jobs !== 0 || recovered_jobs !== 0 || failed_jobs !== 0 || not_attempted_jobs !== 0) {
      el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
      return;
    }
    el.innerHTML = heading + '<p class="reliability-note">No recent workflows</p>';
    return;
  }
  // Derive rate
  let rateText;
  if (attempted_jobs > 0) {
    const rate = recovered_jobs / attempted_jobs;
    rateText = `${Math.round(rate * 100)}%`;
  } else {
    rateText = 'Not attempted';
  }
  const incomplete = unknown_jobs > 0;
  const warning = incomplete ? '<p class="reliability-note" role="alert">Incomplete: unknown historical data</p>' : '';
  const rows = [
    `Latest ${sample_count} workflows (up to 20)`,
    `Implementer jobs: ${implementer_jobs}`,
    `Observed: ${observed_jobs}`,
    `Unknown: ${unknown_jobs}`,
    `Attempted: ${attempted_jobs}`,
    `Recovered: ${recovered_jobs}`,
    `Failed: ${failed_jobs}`,
    `Not attempted: ${not_attempted_jobs}`,
    `Recovery rate: ${rateText}`,
  ].map(t => `<span class="reliability-note">${safe(t)}</span>`).join('');
  el.innerHTML = `${heading}${warning}<div class="reliability-grid">${rows}</div><p class="reliability-note">Only recorded completion continuations; workflow window excludes standalone. Missing historical telemetry is not zero. Counters are not proof of code correctness.</p>`;
}

function handleValidationWorkflowClick(event) {
  const target = event.target;
  const root = event.currentTarget;
  const trigger = target && typeof target.closest === 'function'
    ? target.closest('button.workflow-link[data-workflow-id]') : null;
  if (!trigger || !root || !root.contains(trigger)) return;
  const id = trigger.dataset.workflowId;
  if (typeof id === 'string' && /^[0-9a-f]{10}$/.test(id)) openWorkflow(id);
}

function renderValidationDiagnostics(jobs) {
  const el = $('validation-diagnostics-body');
  if (!el) return;
  // The stable accessible heading lives outside this refresh root.
  const heading = '';
  if (jobs === null || jobs === undefined) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (!Array.isArray(jobs)) {
    el.innerHTML = heading + '<p class="reliability-note">Unavailable</p>';
    return;
  }
  if (jobs.length === 0) {
    el.innerHTML = heading + '<p class="reliability-note">No recent validation jobs</p>';
    return;
  }
  const VALID_OUTCOMES = new Set(['not_attempted','passed','nonzero','timeout','invalid_request','execution_error','suppressed','unknown']);
  const VALID_RECOVERY = new Set(['not_attempted','failed','recovered']);
  const OUTCOME_LABELS = {not_attempted:'Not attempted',passed:'Passed',nonzero:'Nonzero',timeout:'Timeout',invalid_request:'Invalid request',execution_error:'Execution error',suppressed:'Suppressed',unknown:'Unknown'};
  const RECOVERY_LABELS = {not_attempted:'Not attempted',failed:'Failed',recovered:'Recovered'};
  function esc(v) { return safe(v); }
  function isSafeInt(v) { return typeof v === 'number' && Number.isInteger(v) && v >= 0 && v <= 1000000; }
  const WF_ID_RE = /^[0-9a-f]{10}$/;
  const eligible = jobs.filter(j => j && typeof j === 'object' &&
    (j.role === 'implementer' || j.role === 'tester'));
  if (!eligible.length) {
    el.innerHTML = '<p class="reliability-note">No recent validation jobs</p>';
    return;
  }
  const rows = eligible.slice(0, 20).map(j => {
    const role = typeof j.role === 'string' ? j.role : '';
    const stage = typeof j.stage === 'string' ? j.stage : '';
    const project = typeof j.project === 'string' ? j.project : '';
    const label = stage ? (project ? stage + '/' + project : stage) : (project || '\u2014');
    // Revalidate triple
    let req = j.validation_request_count, exe = j.validation_execution_count, outcome = j.validation_last_outcome;
    let tripleOk = isSafeInt(req) && isSafeInt(exe) && exe <= req && typeof outcome === 'string' && VALID_OUTCOMES.has(outcome);
    if (tripleOk) {
      if (req === 0 && outcome !== 'not_attempted') tripleOk = false;
      if (req > 0 && outcome === 'not_attempted') tripleOk = false;
      if ((outcome === 'passed' || outcome === 'nonzero' || outcome === 'timeout') && exe < 1) tripleOk = false;
    }
    if (!tripleOk) { req = null; exe = null; outcome = null; }
    // Revalidate recovery pair
    let recCount = j.validation_recovery_attempted, recState = j.validation_recovery_outcome;
    if (role === 'implementer') {
      let recOk = typeof recCount === 'number' && Number.isInteger(recCount) && (recCount === 0 || recCount === 1) && typeof recState === 'string' && VALID_RECOVERY.has(recState);
      if (recOk) {
        if (recCount === 0 && recState !== 'not_attempted') recOk = false;
        if (recCount === 1 && recState === 'not_attempted') recOk = false;
      }
      if (!recOk) { recCount = null; recState = null; }
    } else { recCount = null; recState = null; }
    // Display mappings
    const reqText = req === null ? '\u2014' : String(req);
    const exeText = exe === null ? '\u2014' : String(exe);
    let outcomeText;
    if (outcome === null) outcomeText = 'Not recorded';
    else if (outcome === 'not_attempted') outcomeText = 'Not attempted';
    else outcomeText = OUTCOME_LABELS[outcome] || 'Not recorded';
    let recText;
    if (role === 'tester') recText = 'Not applicable';
    else if (recState === null) recText = 'Not recorded';
    else recText = RECOVERY_LABELS[recState] || 'Not recorded';
    const wfId = typeof j.workflow_id === 'string' ? j.workflow_id : '';
    const wfValid = WF_ID_RE.test(wfId);
    const labelHtml = wfValid
      ? `<button type="button" class="workflow-link" data-workflow-id="${esc(wfId)}" aria-label="${esc(label)} (workflow ${esc(wfId)})">${esc(label)}</button>`
      : esc(label);
    return `<tr><td>${labelHtml}</td><td>${esc(reqText)}</td><td>${esc(exeText)}</td><td>${esc(outcomeText)}</td><td>${esc(recText)}</td></tr>`;
  }).join('');
  const note = 'Sandbox executions are not proof that tests passed. Historical data may not be recorded.';
  el.innerHTML = heading + `<table class="reliability-table"><thead><tr><th>Stage/project</th><th>Requests</th><th>Executions</th><th>Last outcome</th><th>Validation recovery</th></tr></thead><tbody>${rows}</tbody></table><p class="reliability-note">${esc(note)}</p>`;
}
