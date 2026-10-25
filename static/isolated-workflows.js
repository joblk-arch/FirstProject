/* Separate read-only view. No actions, fetches, links or event handlers. */
function renderIsolatedWorkflows(payload) {
  const root = document.getElementById('isolated-workflow-status');
  if (!root) return;
  root.replaceChildren();
  if (!payload || payload.read_only !== true || !Array.isArray(payload.sources)) {
    root.textContent = 'Isolated workflow data unavailable.';
    return;
  }
  const sources = payload.sources.slice(0, 8);
  const available = sources.filter(source => source && source.read_only === true &&
    source.availability === 'available' && Array.isArray(source.jobs));
  // Prefer actual running work. A failed stage with queued downstream jobs is
  // stopped, not an active queued build. Never delete or change queue records.
  const running = available.filter(source => source.jobs.some(job => job.status === 'running'));
  const queued = available.filter(source => source.jobs.some(job => job.status === 'queued') &&
    !source.jobs.some(job => ['failed', 'blocked', 'cancelled', 'discarded', 'archived'].includes(job.status)));
  const current = running.length ? running[running.length - 1] : queued[queued.length - 1];
  if (!current) {
    root.textContent = sources.some(source => !source || source.read_only !== true ||
      source.availability !== 'available' || !Array.isArray(source.jobs))
      ? 'Current status unavailable; no stale status shown.' : 'No active isolated builds.';
    return;
  }
  for (const source of [current]) {
    const section = document.createElement('div');
    const heading = document.createElement('strong');
    heading.textContent = `${source.label} — read-only`;
    section.appendChild(heading);
    if (source.read_only !== true || source.availability !== 'available' || !Array.isArray(source.jobs)) {
      const notice = document.createElement('p');
      notice.textContent = 'Current status unavailable; no stale status shown.';
      section.appendChild(notice);
    } else {
      for (const job of source.jobs.slice(0, 4)) {
        const line = document.createElement('p');
        line.textContent = `${job.role}: ${job.status} · ${job.id}`;
        section.appendChild(line);
      }
    }
    root.appendChild(section);
  }
}
