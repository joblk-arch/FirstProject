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

- **Dedicated Caddy reverse proxy** (deployed): The `dashboard-tls-proxy`
  service (Caddy) runs in a container on the `dashboard-tls-proxy` Docker
  network with a fixed IP (`172.28.0.2`). It sets `X-Forwarded-Proto: https`
  on upstream requests. The dashboard trusts this exact source IP, so the
  `Secure` flag is set for all requests arriving via the proxy.

- **Tailscale CGNAT** (direct path): If Tailscale Serve connects directly to
  the dashboard container from the CGNAT range (`100.64.0.0/10`), the
  `X-Forwarded-Proto` header is also trusted.

- **Loopback** (local proxy): A proxy on the same host connecting from
  `127.0.0.1` or `::1` is trusted.

- **Any other address**: The `Secure` flag is **not** set. The session still
  works over plain HTTP, but the browser will not enforce HTTPS-only cookie
  delivery.

## Tailscale HTTPS Deployment

### Topology

```
Remote user
    │  HTTPS (TLS terminated by Tailscale Serve on the M1)
    ▼
M1 Tailscale Serve — backend → http://127.0.0.1:8444
    │  HTTP over host loopback
    ▼
M1 Caddy container (dashboard-tls-proxy, port 8444)
    │  HTTP + X-Forwarded-Proto: https (via dashboard-tls-proxy Docker network)
    ▼
Dashboard container (172.28.0.x, port 8080)
```

- **Tailscale Serve** runs on the **M1**, as confirmed by the active Serve
  configuration. It terminates TLS and forwards HTTP to loopback.
- **Caddy** runs on the M1 in a dedicated Docker container. Its port 8444 is
  published only on `127.0.0.1`.
- The dashboard container is reachable from Caddy via the internal
  `dashboard-tls-proxy` Docker network (Caddy's source IP: `172.28.0.2`).

### Security Boundary

Three independent layers prevent LAN clients from spoofing the HTTPS trust:

| Layer | Mechanism |
|-------|-----------|
| Interface binding | Port 8444 is published only on `127.0.0.1`; LAN and direct-Ethernet clients cannot reach it. |
| Docker network isolation | The proxy has a fixed IP (`172.28.0.2`) on a dedicated bridge network. Only the dashboard and Caddy exist on this network. |
| Exact source-IP check | The dashboard trusts `X-Forwarded-Proto` only from `172.28.0.2`, loopback, or Tailscale CGNAT. LAN clients appearing as the Docker bridge gateway (`172.17.0.1`) are not trusted. |

No shared-secret header is used. Tailscale Serve does not inject arbitrary
headers; the `X-Forwarded-Proto: https` header is set by Caddy (via
`header_up`), not by Tailscale.

### Required Tailscale Serve Configuration Change

On the **M1**, update the Tailscale Serve backend target:

```
Before: http://192.168.68.68:8088  (or whatever the previous target was)
After:  http://127.0.0.1:8444
```

This is the only external configuration change required. No Tailscale
configuration is stored in this repository; the change is made on the M1
via:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8444
```

> **Note:** The `--https` port must match the port already configured for
> Tailscale Serve on the M1. If a different external HTTPS port was previously
> in use, substitute it for `--https`; the loopback backend remains on 8444.

Verify the Serve configuration took effect:

```bash
tailscale serve status
```

Expected output shows the HTTPS handler on port 8443 proxying to
`http://127.0.0.1:8444`.

### Safe Deploy Order

1. **Deploy the Caddy proxy** (from this repository, on the M1):
   ```bash
   docker compose up -d dashboard-tls-proxy
   ```
   Wait for healthy: `docker inspect --format='{{.State.Health.Status}}' $(docker compose ps -q dashboard-tls-proxy)`

2. **Verify the proxy path** (from the M1):
   ```bash
   curl -s http://127.0.0.1:8444/health
   # Expected: {"status":"ok"}
   ```

3. **Update Tailscale Serve on the M1** to point at `http://127.0.0.1:8444`.

4. **Verify end-to-end** (from a remote device on the tailnet):
   ```bash
   curl -sk https://<tailscale-hostname>/health
   # Expected: {"status":"ok"}
   ```

5. **Verify Secure cookie** (from a remote browser):
   - Log in via the Tailscale HTTPS URL.
   - Confirm the session cookie has the `Secure` flag (browser dev tools →
     Application → Cookies).

### Verification Commands

```bash
# Proxy is healthy (from M1):
curl -s http://127.0.0.1:8444/health

# Dashboard is healthy (from M1, LAN path):
curl -s http://192.168.68.68:8088/health

# Proxy container status:
docker compose ps dashboard-tls-proxy

# Confirm the published port is bound to the correct interface:
ss -tlnp | grep 8444
# Expected: 127.0.0.1:8444 (NOT 0.0.0.0:8444 or any LAN address)

# Confirm the Docker network isolation:
docker network inspect dashboard-tls-proxy --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'
# Expected: only dashboard-tls-proxy (172.28.0.2) and dashboard
```

### Rollback

If the Tailscale HTTPS path needs to be reverted:

1. **Revert Tailscale Serve on the M1** to the previous backend target
   (e.g., `http://192.168.68.68:8088`).
2. **Stop the Caddy proxy** (optional; it is harmless if left running):
   ```bash
   docker compose stop dashboard-tls-proxy
   ```
3. The LAN HTTP path (`192.168.68.68:8088`) is unaffected at all times.
   Sessions over plain HTTP simply lack the `Secure` flag.

To fully remove the proxy:
```bash
docker compose down dashboard-tls-proxy
docker volume rm $(docker compose config --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['volumes']['caddy_data']['name'])")
docker volume rm $(docker compose config --format json | python3 -c "import sys,json; print(json.load(sys.stdin)['volumes']['caddy_config']['name'])")
docker network rm dashboard-tls-proxy
```
Then remove the `dashboard-tls-proxy` service, the `dashboard-tls-proxy`
network, the `caddy_data`/`caddy_config` volumes, and the
`TRUSTED_PROXY_IP` environment variable from `compose.yaml`, and delete
the `Caddyfile`.

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
