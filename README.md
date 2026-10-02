# Local AI Operations Dashboard

Private, read-only visibility into local agent workflows, project activity,
model usage, stage duration, and recorded tokens. The browser never receives
the agent gateway credential. Approval, merge, push, deployment, prompts, and
arbitrary execution are deliberately excluded.

The dashboard is served on the M1 by Docker and reads aggregated metrics from
the authenticated agent gateway. Token accounting starts after the metrics
instrumentation is deployed; historical jobs remain explicitly unrecorded.

## Start Build

The dashboard includes a "Start Build" form that lets operators and admins
launch new agent workflows through the server-side proxy. The gateway bearer
token and internal URL are never exposed to the browser.

### Configuration

| Variable | Description |
|----------|-------------|
| `AGENT_GATEWAY_ALLOWED_PROJECTS` | Comma-separated list of project names allowed for build submission (e.g. `firstproject,secondproject`) |

### Role Requirements

| Role | Can view dashboard | Can start builds |
|------|-------------------|-----------------|
| `viewer` | Yes | No (form disabled) |
| `operator` | Yes | Yes |
| `admin` | Yes | Yes |

If no authentication mechanism is configured (neither `DASHBOARD_USERS_FILE`
nor `DASHBOARD_PASSWORD_FILE` exists), all non-`/health` paths return 401 and
the build form is unreachable.

### Environment Variables

See `.env.example` for a full reference with safe placeholder values.
