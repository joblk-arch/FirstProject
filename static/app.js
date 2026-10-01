const $ = (id) => document.getElementById(id);
const fmt = (n) => new Intl.NumberFormat().format(n || 0);
const duration = (n) => n == null ? '—' : n < 60 ? `${n}s` : `${Math.floor(n / 60)}m ${n % 60}s`;
const safe = (s) => String(s ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const dialog = $('workflow-dialog');
const workflowContent = $('workflow-content');

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
  $('updated').textContent = `Updated ${new Date(data.generated_at).toLocaleTimeString()}`;
  $('error').hidden = true;
}

function stageCard(stage,index) {
  const tokens = stage.total_tokens == null ? 'Not recorded' : `${fmt(stage.total_tokens)} total`;
  const report = stage.report ? `<details><summary>Stage report</summary><pre class="report">${safe(stage.report)}</pre></details>` : '<p class="muted-copy">No stage report available.</p>';
  return `<article class="stage-card"><div class="stage-order" aria-hidden="true">${index+1}</div><div class="stage-body"><div class="stage-head"><div><span class="stage-role">${safe(stage.stage||stage.role)}</span><strong>${safe(stage.role||'agent')}</strong></div><span class="status ${safe(stage.status)}">${safe(stage.status)}</span></div><dl class="stage-meta"><div><dt>Duration</dt><dd>${duration(stage.duration_seconds)}</dd></div><div><dt>Model</dt><dd>${safe(stage.model||'Not recorded')}</dd></div><div><dt>Tokens</dt><dd>${tokens}</dd></div><div><dt>Input / output</dt><dd>${stage.prompt_tokens==null?'—':`${fmt(stage.prompt_tokens)} / ${fmt(stage.completion_tokens)}`}</dd></div></dl>${report}</div></article>`;
}

function renderWorkflow(workflow) {
  $('workflow-title').textContent = workflow.objective || 'Workflow';
  const stages = workflow.stages || [];
  const evidence = workflow.tester_evidence ? `<section class="detail-section"><h3>Tester evidence</h3><pre class="report">${safe(workflow.tester_evidence)}</pre></section>` : '';
  const review = workflow.reviewer_verdict ? `<span class="verdict ${workflow.reviewer_verdict.toLowerCase()}">${safe(workflow.reviewer_verdict)}</span>` : '<span class="hint">Pending</span>';
  const diff = workflow.diff ? `<section class="detail-section"><details><summary>Unmerged Git diff</summary><pre class="diff"><code>${safe(workflow.diff)}</code></pre></details></section>` : '<section class="detail-section empty-detail"><h3>Unmerged Git diff</h3><p>No diff is available for this workflow.</p></section>';
  workflowContent.innerHTML = `<section class="workflow-summary"><div><span>Project</span><strong>${safe(workflow.project||'Unknown')}</strong></div><div><span>Status</span><strong class="status ${safe(workflow.status)}">${safe(workflow.status||'Unknown')}</strong></div><div><span>Duration</span><strong>${duration(workflow.elapsed_seconds)}</strong></div><div><span>Review</span>${review}</div></section><section class="detail-section"><div class="section-title"><h3>Ordered stages</h3><span class="hint">${stages.length} stages</span></div>${stages.length?`<div class="timeline">${stages.map(stageCard).join('')}</div>`:'<p class="empty-detail">No stages are available for this workflow.</p>'}</section>${evidence}${diff}`;
}

async function openWorkflow(workflowId) {
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

async function load() {
  try { const response=await fetch('/api/dashboard',{cache:'no-store'}); if(!response.ok) throw new Error(`Dashboard returned ${response.status}`); render(await response.json()); }
  catch(error) { $('error').textContent=error.message; $('error').hidden=false; $('updated').textContent='Connection issue'; }
}

$('jobs').addEventListener('click',(event)=>{const trigger=event.target.closest('[data-workflow-id]');if(trigger)openWorkflow(trigger.dataset.workflowId);});
workflowContent.addEventListener('click',(event)=>{const retry=event.target.closest('[data-retry-id]');if(retry)openWorkflow(retry.dataset.retryId);});
$('workflow-close').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',(event)=>{if(event.target===dialog)dialog.close();});
$('refresh').addEventListener('click',load);
load();
setInterval(load,15000);
