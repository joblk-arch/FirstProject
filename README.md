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

### Cluster Health: External Network Prerequisite

The dashboard joins the external Docker network `m1-agent-repo_chat` to reach
the Telegram bot's health endpoint at `http://telegram-bot:8080`.

#### Prerequisites

1. The `m1-agent-repo_chat` network must exist before running `docker compose up`:

   ```bash
   docker network inspect m1-agent-repo_chat
   ```

2. The `telegram-bot` container must be running and attached to that network.

#### Safe Redeploy Order

1. Ensure `telegram-bot` is healthy on `m1-agent-repo_chat`.
2. `docker compose up -d dashboard` (recreates only the dashboard; the
   external network is not touched).
3. Verify: `curl -s http://192.168.68.68:8088/api/cluster-health` shows
   `telegram-bot` as `healthy`.

If the Telegram bot is down, the dashboard degrades gracefully: the
`telegram-bot` entry shows `offline` and the overall status is `degraded`.
No other service is affected.

#### Security

- The Telegram health port is **not** published to the host or LAN.
- The internal URL (`http://telegram-bot:8080`) never appears in the
  `/api/cluster-health` API response.
- No tokens or secrets are exposed in the health payload.

## Environment Variables

See `.env.example` for a full reference with safe placeholder values.
