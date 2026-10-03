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

## Session Authentication & Trusted Proxy

The dashboard uses session-based authentication for browser access and retains
Basic Auth for API/automation clients.

### Cookie Flags

| Flag | Value | Rationale |
|------|-------|-----------|
| `HttpOnly` | `true` | Prevents JavaScript access to the session cookie |
| `SameSite` | `Lax` | Prevents cross-site cookie sending on sub-requests; per-session CSRF tokens protect state-changing browser requests |
| `Path` | `/` | Cookie scoped to the entire application |
| `Max-Age` | `3600` | Browser discards the cookie after 1 hour |
| `Secure` | conditional | Set only when the connection is trusted HTTPS (see below) |

### Trusted Proxy & Secure Flag

The `Secure` cookie flag is set only when the dashboard can confirm the
original client connection was HTTPS. This is determined as follows:

1. **Direct HTTPS**: If the request arrives with `scheme=https` (e.g., the
   container is behind a TLS-terminating proxy that sets the scheme), `Secure`
   is set.

2. **Trusted reverse proxy**: If the request arrives over HTTP but carries
   `X-Forwarded-Proto: https`, the forwarded header is honored **only** when
   the immediate TCP peer (the reverse proxy) is a trusted address:
   - Loopback: `127.0.0.1` or `::1`
   - Tailscale CGNAT: `100.64.0.0/10` (i.e., `100.64.x.x` through `100.127.x.x`)

3. **Untrusted peers**: If the immediate peer is any other address, the
   `X-Forwarded-Proto` header is **ignored** and `Secure` is **not** set.
   This prevents a malicious intermediary from injecting the header.

### Deployment Requirements

- **Tailscale Serve** (recommended): The Tailscale proxy connects from the
  Tailscale CGNAT range, so `X-Forwarded-Proto: https` is trusted and the
  `Secure` flag is set automatically.

- **Local reverse proxy** (e.g., Caddy, nginx on the same host): The proxy
  must connect to the container from loopback (`127.0.0.1`). If the proxy
  runs in a separate container on the Docker network, it will **not** be
  trusted and the `Secure` flag will not be set. This is safe (the cookie
  still works over plain HTTP on a trusted LAN) but the browser will not
  enforce HTTPS-only cookie delivery.

- **Non-Tailscale, non-loopback proxy**: The `Secure` flag will **not** be
  set. The session still works, but the cookie is not restricted to HTTPS.
  If you require the `Secure` flag, ensure your proxy connects from a trusted
  address (loopback or Tailscale CGNAT).

### Session Lifecycle

- **Creation**: On successful login, a 256-bit random session ID is generated.
  Only its SHA-256 hash is stored server-side. Any prior session for the same
  user is invalidated (session fixation resistance).

- **Idle timeout**: 1 hour of inactivity (sliding window, monotonic clock).
  Each authenticated request refreshes the timer.

- **Absolute lifetime**: 24 hours from creation (wall clock). After this, the
  session is destroyed regardless of activity.

- **Logout**: Destroys the server-side session record and clears the cookie.

- **Container restart**: All in-memory sessions are invalidated. Users must
  re-authenticate. No session data persists across restarts.

- **Bounded store**: Maximum 1024 concurrent sessions. Oldest sessions are
  evicted when the limit is reached.

### Basic Auth Compatibility

API clients and automation scripts continue to use HTTP Basic Auth. Basic Auth
requests bypass CSRF protection (credentials are not ambient like cookies).
The `/api/session` endpoint returns `csrf_token: null` for Basic Auth requests.

## Environment Variables

See `.env.example` for a full reference with safe placeholder values.
