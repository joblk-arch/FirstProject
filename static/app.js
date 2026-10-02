const $ = (id) => document.getElementById(id);
const fmt = (n) => new Intl.NumberFormat().format(n || 0);
const duration = (n) => n == null ? '—' : n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${n % 60}s`;
const safe = (s) => String(s ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const dialog = $('workflow-dialog');
const workflowContent = $('workflow-content');
let activeWorkflowId = null;
let sessionIdentity = {username:'',role:'viewer'};
let buildKey = null;
let buildKeyIntent = null;
let buildInFlight = false;

function render(data) {
  const jobs = data.jobs || [], usage = data.usage || [], counts = data.counts || [];
  const active = jobs.filter((j) => ['queued', 'running'].includes(j.status)).length;
  const failed = jobs.filter((j) => ['failed', 'blocked'].includes(j.status)).length;
  const tokens = usage.reduce((n, x) => n + (x.total_tokens || 0), 0);
  $('metrics').innerHTML = [['Active work',active],['Projects',data.projects.length],['Recorded tokens',fmt(tokens)],['Needs attention',failed]].map(([key,value]) => `<article class="metric"><span>${key}</span><strong>${value}</strong></article>`).join('');
  $('jobs').innerHTML = jobs.length ? jobs.slice(0,20).map((job) => {
    const label = safe(job.stage || job.role);
    const stage = job.workflow_id ? `<button class="workflow-link" type="button" data-workflow-id="${safe(job.workflow_id)}" aria-label="Open ${label} workflow details">${label}</button>` : label;
    return `<tr><td>${stage}</td><td>${safe(job.project)}</td><td><span class="status ${safe(job.status)}">${safe(job.status)}</span></td><td>${safe(job.model || 'Not recorded')}</td><td>${job.total_tokens == null ? '—' : fmt(job.total_tokens)}</td><td>${duration(job.duration_seconds)}</td></tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">No agent jobs yet.</td></tr>';
  const byProject = Object.groupBy ? Object.groupBy(counts,(x) => x.project) : counts.reduce((all,x) => ((all[x.project] ??= []).push(x),all),{});
  $('projects').innerHTML = data.projects.map((project) => { const rows=byProject[project.name]||[]; const total=rows.reduce((n,x)=>n+x.count,0); const running=rows.filter((x)=>['running','queued'].includes(x.status)).reduce((n,x)=>n+x.count,0); const width=Math.min(100,total?Math.max(5,running/total*100):0); return `<div class="item"><div class="item-row"><strong>${safe(project.name)}</strong><span>${total} jobs</span></div><small>${safe(project.default_branch)} · ${running} active</small><div class="bar"><i style="width:${width}%"></i></div></div>`; }).join('');
  $('models').innerHTML = usage.length ? usage.map((item) => `<div class="item"><div class="item-row"><strong>${safe((item.model||'Unknown').split('/').pop())}</strong><span>${fmt(item.total_tokens)}</span></div><small>${safe(item.project)} · ${item.jobs} jobs · ${fmt(item.prompt_tokens)} in / ${fmt(item.completion_tokens)} out</small></div>`).join('') : '<div class="empty">Token accounting begins with the next agent job.</div>';
  const workflows = Array.isArray(data.recent_workflows) ? data.recent_workflows : [];
  const sorted = workflows.slice().sort((a, b) => {
    const ta = Date.parse(a.created_at) || 0;
    const tb = Date.parse(b.created_at) || 0;
    return tb - ta;
  });
  $('workflows').innerHTML = sorted.length ? sorted.map((w) => {
    const id = w.id ? safe(w.id) : '';
    const objective = w.objective != null ? safe(w.objective) : '—';
    const status = w.overall != null ? safe(w.overall) : 'unknown';
    const origin = w.origin != null ? safe(w.origin) : '—';
    const models = Array.isArray(w.models) ? w.models.map((m) => safe(m)).join(', ') : (w.models != null ? safe(w.models) : '—');
    const stageCounts = w.stage_counts != null ? (typeof w.stage_counts === 'object' ? Object.entries(w.stage_counts).map(([k, v]) => `${safe(k)}:${safe(v)}`).join(' ') : safe(w.stage_counts)) : '—';
    const tokens = w.total_tokens == null ? '—' : `${fmt(w.prompt_tokens)} / ${fmt(w.completion_tokens)} / ${fmt(w.total_tokens)}`;
    const created = w.created_at ? new Date(w.created_at).toLocaleString() : '—';
    const label = w.project != null ? safe(w.project) : 'Unknown';
    const link = id ? `<button class="workflow-link" type="button" data-workflow-id="${id}" aria-label="Open ${label} workflow details">${label}</button>` : label;
    return `<tr><td>${link}</td><td>${objective}</td><td><span class="status ${status}">${status}</span></td><td>${origin}</td><td>${models}</td><td>${stageCounts}</td><td>${tokens}</td><td>${created}</td></tr>`;
  }).join('') : '<tr><td colspan="8" class="empty">No workflows recorded yet.</td></tr>';
  $('updated').textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
  $('error').hidden = true;
}

function stageCard(stage,index) {
  const tokens = stage.total_tokens == null ? 'Not recorded' : `${fmt(stage.total_tokens)} total`;
  const report = stage.report ? `<details><summary>Stage report</summary><pre class="report">${safe(stage.report)}</pre></details>` : '<p class="muted-copy">No stage report available.</p>';
  return `<article class="stage-card"><div class="stage-order" aria-hidden="true">${index+1}</div><div class="stage-body"><div class="stage-head"><div><span class="stage-role">${safe(stage.stage||stage.role)}</span><strong>${safe(stage.role||'agent')}</strong></div><span class="status ${safe(stage.status)}">${safe(stage.status)}</span></div><dl class="stage-meta"><div><dt>Duration</dt><dd>${duration(stage.duration_seconds)}</dd></div><div><dt>Model</dt><dd>${safe(stage.model||'Not recorded')}</dd></div><div><dt>Tokens</dt><dd>${tokens}</dd></div><div><dt>Input / output</dt><dd>${stage.prompt_tokens==null?'—':`${fmt(stage.prompt_tokens)} / ${fmt(stage.completion_tokens)}`}</dd></div></dl>${report}</div></article>`;
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
  const diff = workflow.diff ? `<section class="detail-section"><details><summary>Unmerged Git diff</summary><pre class="diff"><code>${safe(workflow.diff)}</code></pre></details></section>` : '<section class="detail-section empty-detail"><h3>Unmerged Git diff</h3><p>No diff is available for this workflow.</p></section>';
  workflowContent.innerHTML = `<section class="workflow-summary"><div><span>Project</span><strong>${safe(workflow.project||'Unknown')}</strong></div><div><span>Status</span><strong class="status ${safe(workflow.status)}">${safe(workflow.status||'Unknown')}</strong></div><div><span>Duration</span><strong>${duration(workflow.elapsed_seconds)}</strong></div><div><span>Review</span>${review}</div></section>${workflowActions(workflow)}<section class="detail-section"><div class="section-title"><h3>Ordered stages</h3><span class="hint">${stages.length} stages</span></div>${stages.length?`<div class="timeline">${stages.map(stageCard).join('')}</div>`:'<p class="empty-detail">No stages are available for this workflow.</p>'}</section>${evidence}${diff}`;
}

async function openWorkflow(workflowId) {
  activeWorkflowId = workflowId;
  $('workflow-title').textContent = 'Loading workflow…';
  workflowContent.innerHTML = '<div class="drawer-state"><span class="spinner" aria-hidden="true"></span><p>Loading workflow details…</p></div>';
  if (!dialog.open) dialog.showModal();
  try {
    const response = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}`,{cache:'no-store'});
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
  if (status) status.textContent = `Running ${action}…`;
  try {
    const response = await fetch(`/api/workflows/${encodeURIComponent(activeWorkflowId)}/actions/${encodeURIComponent(action)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:activeWorkflowId})});
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

function setBuildDisabled(disabled) {
  $('build-submit').disabled = disabled;
  $('build-project').disabled = disabled;
  $('build-objective').disabled = disabled;
  document.querySelectorAll('#start-build-form input[type="radio"]').forEach((r) => { r.disabled = disabled; });
}

function normalizeBuildIntent(project, objective, reasoning) {
  return `${project}\u0000${objective}\u0000${reasoning}`;
}

function getBuildKey(project, objective, reasoning) {
  const intent = normalizeBuildIntent(project, objective, reasoning);
  if (buildKey && buildKeyIntent === intent) return buildKey;
  buildKeyIntent = intent;
  buildKey = crypto.randomUUID();
  return buildKey;
}

function rotateBuildKey(project, objective, reasoning) {
  const intent = normalizeBuildIntent(project, objective, reasoning);
  buildKeyIntent = intent;
  buildKey = crypto.randomUUID();
}

function clearBuildKey() {
  buildKey = null;
  buildKeyIntent = null;
}

async function startBuild(event) {
  event.preventDefault();
  if (buildInFlight) return;
  const project = $('build-project').value;
  const objective = $('build-objective').value.trim();
  const reasoning = document.querySelector('input[name="reasoning"]:checked')?.value || 'standard';

  if (!project || !objective) {
    setBuildStatus('Please select a project and enter an objective.', true);
    return;
  }

  const idempotencyKey = getBuildKey(project, objective, reasoning);
  buildInFlight = true;
  setBuildDisabled(true);
  setBuildStatus('Submitting…');

  try {
    const response = await fetch('/api/workflows', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project, objective, reasoning, idempotency_key: idempotencyKey}),
    });
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
    $('build-objective').value = '';
    $('build-char-count').textContent = '0/2000';
    clearBuildKey();
    setBuildDisabled(false);
    buildInFlight = false;
    await load();
    if (result.id) openWorkflow(result.id);
  } catch {
    setBuildStatus('Network error. Please try again.', true);
    setBuildDisabled(false);
    buildInFlight = false;
  }
}

$('build-objective').addEventListener('input', (e) => {
  $('build-char-count').textContent = `${e.target.value.length}/2000`;
});
$('start-build-form').addEventListener('submit', startBuild);

async function load() {
  try { const response=await fetch('/api/dashboard',{cache:'no-store'}); if(!response.ok) throw new Error(`Dashboard returned ${response.status}`); render(await response.json()); }
  catch(error) { $('error').textContent=error.message; $('error').hidden=false; $('updated').textContent='Connection issue'; }
}

async function loadSession() {
  const response = await fetch('/api/session',{cache:'no-store'});
  if (!response.ok) throw new Error(`Session returned ${response.status}`);
  sessionIdentity = await response.json();
  $('identity').textContent = `${sessionIdentity.username} · ${sessionIdentity.role}`;
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

$('jobs').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
$('workflows').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
workflowContent.addEventListener('click',(event)=>{const retry=event.target.closest('[data-retry-id]');if(retry)openWorkflow(retry.dataset.retryId);const action=event.target.closest('[data-workflow-action]');if(action)runWorkflowAction(action.dataset.workflowAction);});
$('workflow-close').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',(event)=>{if(event.target===dialog)dialog.close();});
$('refresh').addEventListener('click',load);
async function initialize(){try{await loadSession();}catch(error){$('identity').textContent='Access unavailable';}await loadAllowedProjects();await load();setInterval(load,15000);}
initialize();
