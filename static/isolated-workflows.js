/* Separate read-only view. No actions, fetches, links or event handlers. */
function renderIsolatedWorkflows(payload) {
  const root = document.getElementById('isolated-workflow-status');
  if (!root) return;
  root.replaceChildren();
  const sources = payload && payload.read_only === true && Array.isArray(payload.sources) ? payload.sources : [];
  if (!sources.length) {
    root.textContent = 'Isolated workflow data unavailable.';
    return;
  }
  for (const source of sources.slice(0, 8)) {
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
