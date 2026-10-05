import hmac
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, HTMLResponse

import asyncio
import time
import urllib.parse


GATEWAY_URL = os.getenv("AGENT_GATEWAY_URL", "http://host.docker.internal:8765").rstrip("/")
M5_HOST_URL = os.getenv("M5_HOST_URL", "")
LMSTUDIO_URL = os.getenv("LMSTUDIO_URL", "")
TELEGRAM_BOT_URL = os.getenv("TELEGRAM_BOT_URL", "")
OPENWEBUI_URL = os.getenv("OPENWEBUI_URL", "")
ROUTER_URL = os.getenv("ROUTER_URL", "")
GATEWAY_KEY_FILE = Path(os.getenv("AGENT_GATEWAY_KEY_FILE", "/run/secrets/agent_gateway_key"))
LMSTUDIO_TOKEN_FILE = Path(os.getenv("LMSTUDIO_TOKEN_FILE", "/run/secrets/lm_studio_token"))
PASSWORD_FILE = Path(os.getenv("DASHBOARD_PASSWORD_FILE", "/run/secrets/dashboard_password"))
USERS_FILE = Path(os.getenv("DASHBOARD_USERS_FILE", "/run/secrets/dashboard_users"))
AUDIT_LOG_FILE = Path(os.getenv("DASHBOARD_AUDIT_LOG_FILE", "/tmp/local-ai-dashboard-audit.jsonl"))
TEMPLATES_FILE = Path(os.getenv("DASHBOARD_TEMPLATES_FILE", "/data/templates.json"))
USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
ALLOWED_PROJECTS = [p.strip() for p in os.getenv("AGENT_GATEWAY_ALLOWED_PROJECTS", "").split(",") if p.strip()]
WORKFLOW_ACTIONS = {"retry", "rereview", "approve", "merge", "push", "cleanup"}
ACTION_ROLES = {
    "retry": {"operator", "admin"},
    "rereview": {"operator", "admin"},
    "approve": {"admin"},
    "merge": {"admin"},
    "push": {"admin"},
    "cleanup": {"admin"},
}
BUILD_ROLES = {"operator", "admin"}
VALID_ROLES = {"viewer", "operator", "admin"}
_audit_lock = threading.Lock()
_templates_lock = threading.Lock()

# --- Session store ---

SESSION_IDLE_TIMEOUT = 3600  # 1 hour
SESSION_ABSOLUTE_LIFETIME = 86400  # 24h hard cap
MAX_SESSIONS = 1024
SESSION_COOKIE_NAME = "dashboard_session"
_session_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_session_counter = 0
_CLEANUP_INTERVAL = 64  # cleanup every N validations

# Explicit allowlist of static file extensions that bypass authentication.
# Only these extensions are served by the StaticFiles mount; anything else
# is treated as a dynamic route requiring authentication.
_STATIC_FILE_EXTENSIONS = frozenset({
    "css", "js", "mjs", "json",
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp",
    "woff", "woff2", "ttf", "eot", "otf",
    "map", "txt", "webmanifest",
})


def _hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _create_session(username: str, role: str) -> str:
    """Create a new session. Rotates: invalidates any existing session for the same user."""
    global _session_counter
    session_id = secrets.token_bytes(32).hex()
    session_hash = _hash_session_id(session_id)
    csrf_token = secrets.token_hex(32)
    now_mono = time.monotonic()
    now_wall = time.time()
    record = {
        "session_hash": session_hash,
        "username": username,
        "role": role,
        "csrf_token": csrf_token,
        "created_at": now_mono,
        "last_seen": now_mono,
        "wall_created": now_wall,
    }
    with _session_lock:
        # Invalidate existing sessions for this user (rotation / fixation resistance)
        to_remove = [k for k, v in _sessions.items() if v["username"] == username]
        for k in to_remove:
            del _sessions[k]
        # Enforce max sessions by evicting oldest
        while len(_sessions) >= MAX_SESSIONS:
            oldest_key = min(_sessions, key=lambda k: _sessions[k]["created_at"])
            del _sessions[oldest_key]
        _sessions[session_hash] = record
        _session_counter += 1
    return session_id


def _validate_session(session_id: str) -> dict | None:
    """Validate a session ID. Returns the session record or None if expired/missing."""
    global _session_counter
    session_hash = _hash_session_id(session_id)
    with _session_lock:
        record = _sessions.get(session_hash)
        if record is None:
            return None
        now_mono = time.monotonic()
        now_wall = time.time()
        # Check idle timeout
        if now_mono - record["last_seen"] > SESSION_IDLE_TIMEOUT:
            del _sessions[session_hash]
            return None
        # Check absolute lifetime
        if now_wall - record["wall_created"] > SESSION_ABSOLUTE_LIFETIME:
            del _sessions[session_hash]
            return None
        # Refresh last_seen
        record["last_seen"] = now_mono
        # Lazy cleanup
        _session_counter += 1
        if _session_counter % _CLEANUP_INTERVAL == 0:
            _cleanup_sessions_locked(now_mono, now_wall)
        return dict(record)


def _destroy_session(session_id: str) -> None:
    session_hash = _hash_session_id(session_id)
    with _session_lock:
        _sessions.pop(session_hash, None)


def _cleanup_sessions_locked(now_mono: float, now_wall: float) -> None:
    """Remove expired sessions. Caller must hold _session_lock."""
    to_remove = [
        k for k, v in _sessions.items()
        if now_mono - v["last_seen"] > SESSION_IDLE_TIMEOUT
        or now_wall - v["wall_created"] > SESSION_ABSOLUTE_LIFETIME
    ]
    for k in to_remove:
        del _sessions[k]


# Fixed IP of the dedicated Caddy reverse-proxy container on the
# dashboard-tls-proxy Docker network. This is the only non-loopback,
# non-CGNAT address whose X-Forwarded-Proto header we trust.
TRUSTED_PROXY_IP = os.getenv("TRUSTED_PROXY_IP", "172.28.0.2")


def _is_trusted_proxy(host: str) -> bool:
    """Constrain which peers' X-Forwarded-Proto we trust.

    Trusted sources:
    - Loopback (127.0.0.1, ::1): local Tailscale Serve on the host.
    - Tailscale CGNAT (100.64.0.0/10): direct Tailscale Serve path.
    - TRUSTED_PROXY_IP (default 172.28.0.2): the dedicated Caddy reverse
      proxy on the internal dashboard-tls-proxy Docker network. This is the
      fixed source address of the proxy container; normal LAN clients
      reaching the published port 192.168.68.68:8088 appear as the Docker
      bridge gateway (e.g. 172.17.0.1) and are NOT trusted.
    """
    if host in ("127.0.0.1", "::1"):
        return True
    if host == TRUSTED_PROXY_IP:
        return True
    if host.startswith("100."):
        # Tailscale CGNAT range: 100.64.0.0/10
        parts = host.split(".")
        if len(parts) == 4:
            try:
                second_octet = int(parts[1])
                if 64 <= second_octet <= 127:
                    return True
            except (ValueError, IndexError):
                pass
    return False


def _is_trusted_https(request: Request) -> bool:
    """Determine if the original client connection was HTTPS.

    Trusts X-Forwarded-Proto only from known proxy addresses (loopback or
    Tailscale CGNAT). Untrusted peers' forwarded headers are ignored.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded == "https":
        client_host = request.client.host if request.client else ""
        if _is_trusted_proxy(client_host):
            return True
    return False


def _set_session_cookie(response: Response, session_id: str, request: Request) -> None:
    is_https = _is_trusted_https(request)
    # max_age is exactly 3600 (1 hour) as explicitly required.
    # The server-side sliding idle timeout (also 1h) is the primary expiration
    # mechanism; the cookie max_age ensures the browser discards the cookie
    # after one hour even if the user navigates away and returns.
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=SESSION_IDLE_TIMEOUT,
        secure=is_https,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# --- Login rate limiting (simple in-memory per-IP throttle) ---

LOGIN_RATE_LIMIT_MAX = 10  # max attempts per window
LOGIN_RATE_LIMIT_WINDOW = 60  # seconds
_login_attempts: dict[str, list[float]] = {}
_login_rate_lock = threading.Lock()


def _check_login_rate_limit(client_ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    Also sweeps stale keys (whose newest timestamp is older than the window)
    to bound the dictionary in long-running processes behind NATs.
    """
    now = time.time()
    with _login_rate_lock:
        # Sweep stale keys to prevent unbounded growth
        stale_keys = [
            ip for ip, timestamps in _login_attempts.items()
            if timestamps and now - timestamps[-1] >= LOGIN_RATE_LIMIT_WINDOW
        ]
        for ip in stale_keys:
            del _login_attempts[ip]

        attempts = _login_attempts.get(client_ip, [])
        # Prune old attempts outside the window
        attempts = [t for t in attempts if now - t < LOGIN_RATE_LIMIT_WINDOW]
        if len(attempts) >= LOGIN_RATE_LIMIT_MAX:
            _login_attempts[client_ip] = attempts
            return False
        attempts.append(now)
        _login_attempts[client_ip] = attempts
        return True


def _is_static_file_path(path: str) -> bool:
    """Check if a path is a known static file using an explicit extension allowlist."""
    if path.startswith("/api/"):
        return False
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return False
    ext = last_segment.rsplit(".", 1)[-1].lower()
    return ext in _STATIC_FILE_EXTENSIONS

# --- Cluster Health configuration ---

HEALTH_TIMEOUT = float(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))
HEALTH_CONCURRENCY = int(os.getenv("HEALTH_CHECK_CONCURRENCY", "8"))

HEALTH_SERVICES: list[dict] = [
    {"name": "m5-inference", "url_env": "LMSTUDIO_URL", "check_type": "lm_studio"},
    {"name": "lm-studio", "url_env": "LMSTUDIO_URL", "check_type": "lm_studio"},
    {"name": "agent-gateway", "url_env": "GATEWAY_URL", "check_type": "gateway"},
    {"name": "dashboard", "url_env": None, "check_type": "self"},
    {"name": "telegram-bot", "url_env": "TELEGRAM_BOT_URL", "check_type": "http"},
    {"name": "open-webui", "url_env": "OPENWEBUI_URL", "check_type": "http"},
    {"name": "router", "url_env": "ROUTER_URL", "check_type": "http"},
]

# Fixed maximum length for a sanitized LM Studio model identifier. IDs longer
# than this, or containing characters outside the safe set, are dropped.
LMSTUDIO_MODEL_ID_MAX = 200
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]*$")

# Fixed maximum number of running jobs surfaced in the agent queue summary.
# The accurate `running` count is always reported; the list is bounded so a
# pathological gateway cannot bloat the response.
MAX_RUNNING_JOBS = 20


def _normalize_health_url(url: str) -> str | None:
    """Normalize an operator-supplied health URL.

    Returns a normalized http/https URL with trailing slashes stripped, or
    ``None`` if the URL is empty or uses a disallowed scheme. Only ``http`` and
    ``https`` are permitted; any other scheme (``file://``, ``gopher://``, ...)
    is rejected so a misconfigured value cannot trigger a non-HTTP request.
    """
    if not isinstance(url, str):
        return None
    candidate = url.strip().rstrip("/")
    if not candidate:
        return None
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    return candidate


def _sanitize_model_id(raw_id) -> str | None:
    """Bound and sanitize an LM Studio model identifier.

    Policy: the ID must be a non-empty string of at most
    ``LMSTUDIO_MODEL_ID_MAX`` characters containing only ``[A-Za-z0-9/._-]``.
    IDs that fail are dropped (not emitted) so a misconfigured or hostile host
    cannot surface arbitrary strings in the health payload.
    """
    if not isinstance(raw_id, str):
        return None
    candidate = raw_id.strip()
    if not candidate or len(candidate) > LMSTUDIO_MODEL_ID_MAX:
        return None
    if _MODEL_ID_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _service_url(service: dict) -> str:
    """Resolve a service's configured URL from the current module-level value.

    Reading at call time (rather than baking in at import) keeps the health
    checks testable: tests can patch the module-level URL variables.
    """
    env_name = service.get("url_env")
    if env_name is None:
        return ""
    value = globals().get(env_name, "")
    return value if isinstance(value, str) else ""


class StartWorkflowRequest(BaseModel):
    project: str
    objective: str
    reasoning: str = "standard"
    idempotency_key: str


class TemplateSpec(BaseModel):
    goal: str
    acceptance: str
    scope: str = ""
    exclusions: str = ""
    required_tests: str = ""
    notes: str = ""


class Template(BaseModel):
    id: str
    project: str
    name: str
    spec: TemplateSpec
    created_at: str
    updated_at: str


class CreateTemplateRequest(BaseModel):
    project: str
    name: str
    spec: TemplateSpec


app = FastAPI(title="Local AI Operations")
security = HTTPBasic()


def _verify_password(password: str, record: dict) -> bool:
    try:
        role = record["role"]
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["password_hash"])
        iterations = int(record.get("iterations", 600_000))
    except (KeyError, TypeError, ValueError):
        return False
    if role not in VALID_ROLES or iterations < 100_000:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def authenticate_identity(credentials: HTTPBasicCredentials) -> tuple[str, str]:
    if USERS_FILE.is_file():
        try:
            users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("Invalid credentials")
        record = users.get(credentials.username) if isinstance(users, dict) else None
        if not isinstance(record, dict) or not _verify_password(credentials.password, record):
            raise ValueError("Invalid credentials")
        return credentials.username, record["role"]

    expected_password = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    valid = hmac.compare_digest(credentials.username, USERNAME) and hmac.compare_digest(
        credentials.password, expected_password
    )
    if not valid:
        raise ValueError("Invalid credentials")
    return credentials.username, "admin"


def authenticate(credentials: HTTPBasicCredentials) -> str:
    return authenticate_identity(credentials)[0]


def _audit(request: Request, workflow_id: str, action: str, outcome: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_id": uuid.uuid4().hex,
        "actor": getattr(request.state, "username", "unknown"),
        "role": getattr(request.state, "role", "unknown"),
        "workflow_id": workflow_id,
        "action": action,
        "outcome": outcome,
        "source_ip": request.client.host if request.client else None,
    }
    AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock, AUDIT_LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_gateway_key() -> str:
    """Read the gateway bearer key, returning a controlled redacted 502 if absent/unreadable."""
    try:
        return GATEWAY_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        raise HTTPException(status_code=502, detail="Gateway credential unavailable")


# Paths that bypass authentication entirely
_AUTH_EXEMPT_PATHS = {"/health", "/login", "/api/login", "/api/logout"}


# --- Login page (cached at startup to avoid per-request disk I/O) ---
_LOGIN_PAGE_HTML: str | None = None


def _get_login_page_html() -> str:
    """Return the cached login page HTML, reading from disk on first use."""
    global _LOGIN_PAGE_HTML
    if _LOGIN_PAGE_HTML is None:
        _LOGIN_PAGE_HTML = (Path(__file__).parent / "static" / "login.html").read_text(encoding="utf-8")
    return _LOGIN_PAGE_HTML


# --- Middleware ---
# IMPORTANT: Middleware ordering is critical. Starlette's `@app.middleware("http")`
# uses `insert(0, ...)` so the LAST decorated middleware runs FIRST in the request
# chain. The required order (outermost → innermost) is:
#   1. require_authentication  (sets request.state.username/role/csrf_token/auth_method)
#   2. csrf_protection         (reads request.state.csrf_token, enforces X-CSRF-Token)
#   3. _AcknowledgeBodyLimitMiddleware (ASGI-level, added via add_middleware)
# Do NOT reorder these without updating the tests.


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """CSRF protection for state-changing endpoints.

    Basic-auth requests bypass CSRF (documented non-cookie rule: Basic Auth
    credentials are not ambient like cookies, so CSRF is not applicable).
    Session-authenticated requests must present the per-session CSRF token
    in the X-CSRF-Token header, compared in constant time.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return await call_next(request)
    # Auth-exempt paths (login, logout) do not require CSRF
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)
    # Static file paths are not state-changing in practice
    if _is_static_file_path(request.url.path):
        return await call_next(request)
    # Basic Auth requests bypass CSRF
    if getattr(request.state, "auth_method", None) == "basic":
        return await call_next(request)
    # Session-authenticated: require X-CSRF-Token header
    expected = getattr(request.state, "csrf_token", None)
    if expected is None:
        return JSONResponse(status_code=403, content={"detail": "CSRF token missing"})
    provided = request.headers.get("x-csrf-token", "")
    if not provided or not hmac.compare_digest(provided, expected):
        return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
    return await call_next(request)


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """Dual-path authentication: session cookie (browser) or Basic Auth (API).

    - Session cookie path: validates the opaque session ID, sets username/role/csrf_token.
    - Basic Auth path: validates credentials directly, sets username/role, csrf_token=None.
    - Unauthenticated browser (Accept: text/html): serves login page, no WWW-Authenticate.
    - Unauthenticated API (Accept: application/json or no Accept): 401 JSON with WWW-Authenticate.
    """
    path = request.url.path

    # Exempt paths
    if path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    # Static files (CSS, JS, images) are accessible without auth
    if _is_static_file_path(path):
        return await call_next(request)

    # --- Path 1: Session cookie (browser) ---
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        session = _validate_session(session_id)
        if session:
            request.state.username = session["username"]
            request.state.role = session["role"]
            request.state.csrf_token = session["csrf_token"]
            request.state.auth_method = "session"
            return await call_next(request)
        # Expired or invalid: destroy it
        _destroy_session(session_id)

    # --- Path 2: Basic Auth (API/automation clients) ---
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            value = auth_header.split(" ", 1)[1]
            username, password = base64.b64decode(value).decode("utf-8").split(":", 1)
            username, role = authenticate_identity(
                HTTPBasicCredentials(username=username, password=password)
            )
            request.state.username = username
            request.state.role = role
            request.state.csrf_token = None
            request.state.auth_method = "basic"
            return await call_next(request)
        except (ValueError, UnicodeDecodeError):
            pass

    # --- Unauthenticated ---
    accept = request.headers.get("accept", "")
    is_browser = "text/html" in accept and "application/json" not in accept

    if is_browser:
        # Serve login page — NO WWW-Authenticate challenge
        return await _serve_login_page(request)
    else:
        # JSON 401 — include WWW-Authenticate for Basic Auth API clients
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Local AI Operations"'},
        )


async def _serve_login_page(request: Request) -> HTMLResponse:
    """Serve the login page for unauthenticated browser navigation."""
    return HTMLResponse(content=_get_login_page_html(), status_code=200)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(payload: LoginRequest, request: Request):
    """Authenticate and create a browser session.

    Uses the existing constant-time PBKDF2 password verification.
    On success, creates a new session (rotating any existing one for the user)
    and sets the session cookie.

    Rate-limited: max 10 attempts per 60 seconds per client IP.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    try:
        username, role = authenticate_identity(
            HTTPBasicCredentials(username=payload.username, password=payload.password)
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    session_id = _create_session(username, role)
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, session_id, request)
    return response


@app.post("/api/logout")
async def logout(request: Request):
    """Invalidate the current session and clear the cookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        _destroy_session(session_id)
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


@app.get("/api/session")
async def session(request: Request):
    """Return the current session identity and CSRF token.

    For session-authenticated requests: returns username, role, and csrf_token.
    For Basic Auth requests: returns username, role, and csrf_token=null.
    """
    csrf_token = getattr(request.state, "csrf_token", None)
    return {
        "username": request.state.username,
        "role": request.state.role,
        "csrf_token": csrf_token,
    }


@app.get("/login")
async def login_page():
    """Serve the login page for browser navigation."""
    return HTMLResponse(content=_get_login_page_html())


def _project_dashboard(raw: dict) -> dict:
    """Project the gateway dashboard payload, adding safe repair visibility.

    Passes through all existing fields unchanged. Adds:
    - System-level: repair_enabled, repair_activated, repair_max_attempts
    - Per-workflow: repair_state (derived) to each entry in recent_workflows

    Missing or malformed repair fields yield None / "none" state, never errors.
    """
    result = dict(raw)

    # System-level repair fields (safely validated)
    result["repair_enabled"] = _safe_bool(raw.get("repair_enabled"))
    result["repair_activated"] = _safe_bool(raw.get("repair_activated"))
    result["repair_max_attempts"] = _safe_nonneg_int(raw.get("repair_max_attempts"))

    # Per-workflow repair_state derivation
    workflows = raw.get("recent_workflows")
    if isinstance(workflows, list):
        projected_workflows = []
        for w in workflows:
            if not isinstance(w, dict):
                continue
            entry = dict(w)
            # Safely extract per-workflow repair fields
            w_enabled = _safe_bool(w.get("repair_enabled"))
            w_activated = _safe_bool(w.get("repair_activated"))
            w_attempts = _safe_nonneg_int(w.get("repair_attempts"))
            w_max = _safe_nonneg_int(w.get("repair_max_attempts"))
            # Fall back to system-level values if per-workflow values are missing
            if w_enabled is None:
                w_enabled = result["repair_enabled"]
            if w_activated is None:
                w_activated = result["repair_activated"]
            if w_max is None:
                w_max = result["repair_max_attempts"]
            w_status = w.get("overall") or w.get("status")
            entry["status"] = w_status
            # Safely project the routing reasons; overrides any raw value from
            # the pass-through dict so prompts/outputs/paths/credentials are
            # never exposed. None lets the UI fall back to a graceful em dash.
            entry["model_reasons"] = _safe_model_reasons(w.get("model_reasons"))
            w_verdict = w.get("reviewer_verdict")
            if w_verdict not in {"APPROVE", "REJECT"}:
                w_verdict = None
            entry["repair_state"] = _derive_repair_state(
                w_enabled, w_activated, w_attempts, w_max, w_status, w_verdict,
            )
            projected_workflows.append(entry)
        result["recent_workflows"] = projected_workflows

    return result


@app.get("/api/dashboard")
async def dashboard():
    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{GATEWAY_URL}/v1/dashboard",
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            return raw
        return _project_dashboard(raw)


def _safe_bool(value) -> bool | None:
    """Return True/False only for actual booleans; None for anything else."""
    if isinstance(value, bool):
        return value
    return None


def _safe_nonneg_int(value) -> int | None:
    """Return a non-negative int; None for missing, negative, or non-int values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _derive_repair_state(
    repair_enabled: bool | None,
    repair_activated: bool | None,
    repair_attempts: int | None,
    repair_max_attempts: int | None,
    status: str | None,
    reviewer_verdict: str | None,
) -> str:
    """Derive a safe, read-only repair state from workflow metadata.

    States:
      - "none": repair not enabled/activated, zero attempts, or unknown status
      - "repairing": actively queued/running correction chain with attempts > 0
      - "ready_after_repair": reviewer approved AND workflow ready-for-approval
      - "rejected_attempts_remaining": authoritative overall is rejected, attempts remain
      - "exhausted": max attempts reached while workflow is still active/rejected
      - "history": terminal workflow state with prior repair attempts (neutral label)

    Terminal states (merged, pushed, archived, failed, blocked, completed) never
    display as actively repairing or falsely claim success.
    Unknown/malformed statuses yield no badge.
    """
    _TERMINAL = frozenset({"merged", "pushed", "archived", "failed", "blocked", "completed"})
    _ACTIVE = frozenset({"queued", "running"})

    # If repair is explicitly disabled or not activated, no repair activity
    if repair_enabled is False or repair_activated is False:
        return "none"
    # If no attempts recorded, no repair activity
    if repair_attempts is None or repair_attempts == 0:
        return "none"
    # Terminal states: show neutral history, never "repairing" or success
    if status in _TERMINAL:
        return "history"
    # Unknown/malformed status: no badge
    if status is None:
        return "none"
    # Success: reviewer approved AND workflow is ready for human approval
    if status == "ready-for-approval" and reviewer_verdict == "APPROVE":
        return "ready_after_repair"
    # Ready-for-approval without APPROVE: cannot confirm success, no badge
    if status == "ready-for-approval":
        return "none"
    # Exhausted: max attempts reached while workflow is still active or rejected
    if repair_max_attempts is not None and repair_attempts >= repair_max_attempts:
        return "exhausted"
    # Rejected: only when the authoritative overall state is "rejected"
    if status == "rejected":
        return "rejected_attempts_remaining"
    # Active: only for genuinely queued/running correction chains
    if status in _ACTIVE:
        return "repairing"
    # Any other unrecognized state: no badge
    return "none"


MODEL_REASON_ALLOWLIST = frozenset({
    "explicit-fast",
    "explicit-deep",
    "reasoning-keyword",
    "long-complex-prompt",
    "default-fast",
    "reasoner-default-deep",
    "auto-mutation-stays-fast",
})


def _safe_model_reason(value) -> str | None:
    """Return the model routing reason only if it is in the exact allow-list.

    Accepts only the known, safe routing reason strings. Unknown, non-string,
    control-character, or oversized values project to None so they never reach
    HTML. Returns None for anything else so the UI can fall back to a graceful
    em dash.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value not in MODEL_REASON_ALLOWLIST:
        return None
    return value


MAX_MODEL_REASONS = len(MODEL_REASON_ALLOWLIST)


def _safe_model_reasons(value) -> list[str] | None:
    """Project a workflow summary's ``model_reasons`` to a safe, bounded list.

    Accepts only a list whose entries are non-empty strings of bounded length
    with no control characters. Never passes through prompts, outputs, errors,
    paths, credentials, or raw arbitrary fields. Returns None when the raw value
    is not a list so the UI can fall back to a graceful em dash.
    """
    if not isinstance(value, list):
        return None
    reasons = []
    seen = set()
    for item in value:
        safe = _safe_model_reason(item)
        if safe is not None and safe not in seen:
            reasons.append(safe)
            seen.add(safe)
        if len(reasons) >= MAX_MODEL_REASONS:
            break
    return reasons


def _project_stage(raw: dict) -> dict:
    """Project a single stage to the strict read-only allow-list."""
    return {
        "stage": raw.get("stage"),
        "role": raw.get("role"),
        "status": raw.get("status"),
        "duration_seconds": raw.get("duration_seconds"),
        "model": raw.get("model"),
        "model_reason": _safe_model_reason(raw.get("model_reason")),
        "prompt_tokens": raw.get("prompt_tokens"),
        "completion_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "report": raw.get("report") if isinstance(raw.get("report"), str) else "",
    }


def _project_workflow(raw: dict) -> dict:
    """Project gateway workflow data to the strict read-only allow-list.

    Only safe, read-only fields are included. Prompts, secrets, credentials,
    internal URLs, filesystem/worktree paths, stack traces, and mutation
    controls are never passed through.
    """
    workflow_raw = raw.get("workflow")
    if not isinstance(workflow_raw, dict):
        workflow_raw = {}
    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list):
        stages_raw = []

    status = workflow_raw.get("overall")
    reviewer_verdict = raw.get("reviewer_verdict")
    if reviewer_verdict not in {"APPROVE", "REJECT"}:
        reviewer_verdict = None

    # Safely extract repair metadata
    repair_enabled = _safe_bool(raw.get("repair_enabled"))
    repair_activated = _safe_bool(raw.get("repair_activated"))
    repair_attempts = _safe_nonneg_int(raw.get("repair_attempts"))
    repair_max_attempts = _safe_nonneg_int(raw.get("repair_max_attempts"))

    result: dict = {
        "id": workflow_raw.get("id"),
        "objective": workflow_raw.get("objective"),
        "project": workflow_raw.get("project"),
        "status": status,
        "elapsed_seconds": workflow_raw.get("elapsed_seconds"),
        "stages": [
            _project_stage(s) for s in stages_raw if isinstance(s, dict)
        ],
        "tester_evidence": raw.get("tester_evidence")
        if isinstance(raw.get("tester_evidence"), str) else None,
        "reviewer_verdict": reviewer_verdict,
        "repair_enabled": repair_enabled,
        "repair_activated": repair_activated,
        "repair_attempts": repair_attempts,
        "repair_max_attempts": repair_max_attempts,
        "repair_state": _derive_repair_state(
            repair_enabled, repair_activated, repair_attempts,
            repair_max_attempts, status, reviewer_verdict,
        ),
    }

    # Diff text only when the gateway safely provides it as a string.
    diff = raw.get("diff")
    if isinstance(diff, str):
        result["diff"] = diff

    return result


@app.get("/api/workflows/allowed-projects")
async def allowed_projects():
    return {"projects": ALLOWED_PROJECTS}


@app.get("/api/workflows/{workflow_id}")
async def workflow_detail(workflow_id: str):
    if re.fullmatch(r"[0-9a-f]{10}", workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(
                f"{GATEWAY_URL}/v1/workflows/{workflow_id}/result",
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Upstream gateway unavailable")

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Upstream gateway error")

    try:
        raw = response.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid upstream response")

    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="Invalid upstream response")

    return _project_workflow(raw)


class WorkflowActionRequest(BaseModel):
    confirm: str


@app.post("/api/workflows/{workflow_id}/actions/{action}")
async def workflow_action(
    workflow_id: str,
    action: str,
    payload: WorkflowActionRequest,
    request: Request,
):
    if re.fullmatch(r"[0-9a-f]{10}", workflow_id) is None or action not in WORKFLOW_ACTIONS:
        raise HTTPException(status_code=404, detail="Workflow action not found")
    if request.state.role not in ACTION_ROLES[action]:
        _audit(request, workflow_id, action, "denied")
        raise HTTPException(status_code=403, detail="Your role cannot perform this action")
    if payload.confirm != workflow_id:
        _audit(request, workflow_id, action, "confirmation_rejected")
        raise HTTPException(status_code=400, detail="Workflow confirmation does not match")

    _audit(request, workflow_id, action, "attempted")

    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(
                f"{GATEWAY_URL}/v1/workflows/{workflow_id}/actions/{action}",
                headers={"Authorization": f"Bearer {key}"},
                json={"confirm": workflow_id},
            )
        except httpx.HTTPError:
            _audit(request, workflow_id, action, "upstream_unavailable")
            raise HTTPException(status_code=502, detail="Upstream gateway unavailable")

    if response.status_code == 409:
        _audit(request, workflow_id, action, "state_rejected")
        raise HTTPException(
            status_code=409,
            detail="Action is not valid for the workflow's current state",
        )
    if response.status_code >= 400:
        _audit(request, workflow_id, action, "upstream_failed")
        raise HTTPException(status_code=502, detail="Upstream gateway error")
    _audit(request, workflow_id, action, "succeeded")
    return {"ok": True, "action": action}


@app.post("/api/workflows")
async def start_workflow(payload: StartWorkflowRequest, request: Request):
    if request.state.role not in BUILD_ROLES:
        _audit(request, "", "start_build", "denied")
        raise HTTPException(status_code=403, detail="Build actions require operator or admin access")

    project = payload.project.strip()
    objective = payload.objective.strip()
    reasoning = payload.reasoning
    idempotency_key = payload.idempotency_key.strip()

    if not project:
        raise HTTPException(status_code=400, detail="Project is required")
    if not objective:
        raise HTTPException(status_code=400, detail="Objective is required")
    if len(objective) > 2000:
        raise HTTPException(status_code=400, detail="Objective must be 2000 characters or fewer")
    if any(ord(c) < 32 for c in objective):
        raise HTTPException(status_code=400, detail="Objective contains invalid control characters")
    if reasoning not in ("standard", "deep"):
        raise HTTPException(status_code=400, detail="Reasoning must be 'standard' or 'deep'")
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", idempotency_key):
        raise HTTPException(status_code=400, detail="Idempotency key must be a valid UUID-shaped value")
    if project not in ALLOWED_PROJECTS:
        raise HTTPException(status_code=400, detail="Project is not in the allowed list")

    _audit(request, "", "start_build", "attempted")

    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                f"{GATEWAY_URL}/v1/workflows",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "project": project,
                    "objective": objective,
                    "reasoning": reasoning == "deep",
                },
            )
        except httpx.HTTPError:
            _audit(request, "", "start_build", "upstream_unavailable")
            raise HTTPException(status_code=502, detail="Upstream gateway unavailable")

    if response.status_code in (200, 202):
        try:
            data = response.json()
        except Exception:
            data = None
        if not isinstance(data, dict):
            _audit(request, "", "start_build", "upstream_failed")
            raise HTTPException(status_code=502, detail="Invalid upstream response")
        workflow_id = data.get("workflow_id")
        overall = data.get("overall")
        status = data.get("status")
        if status == "processing" or overall == "processing":
            _audit(request, workflow_id if isinstance(workflow_id, str) else "", "start_build", "processing")
            return {
                "id": workflow_id if isinstance(workflow_id, str) and re.fullmatch(r"[0-9a-f]{10}", workflow_id) else None,
                "status": "processing",
                "pending": True,
                "project": project,
                "objective": objective,
                "reasoning": reasoning,
            }
        if not isinstance(workflow_id, str) or re.fullmatch(r"[0-9a-f]{10}", workflow_id) is None:
            _audit(request, "", "start_build", "upstream_failed")
            raise HTTPException(status_code=502, detail="Invalid upstream response")
        if not isinstance(overall, str) or not overall:
            _audit(request, "", "start_build", "upstream_failed")
            raise HTTPException(status_code=502, detail="Invalid upstream response")
        _audit(request, workflow_id, "start_build", "succeeded")
        return {
            "id": workflow_id,
            "status": overall,
            "pending": False,
            "project": project,
            "objective": objective,
            "reasoning": reasoning,
        }
    if response.status_code == 401:
        _audit(request, "", "start_build", "gateway_auth_failed")
        raise HTTPException(status_code=502, detail="Gateway authorization failed")
    if response.status_code == 409:
        _audit(request, "", "start_build", "duplicate")
        raise HTTPException(status_code=409, detail="A workflow with this idempotency key already exists")
    _audit(request, "", "start_build", "upstream_failed")
    raise HTTPException(status_code=502, detail="Upstream gateway error")


def _validate_template_text(value: str, field_name: str, min_len: int = 0, max_len: int = 2000) -> str:
    if len(value) < min_len:
        raise HTTPException(status_code=400, detail=f"{field_name} must be at least {min_len} characters")
    if len(value) > max_len:
        raise HTTPException(status_code=400, detail=f"{field_name} must be at most {max_len} characters")
    if any(ord(c) < 32 for c in value):
        raise HTTPException(status_code=400, detail=f"{field_name} contains invalid control characters")
    return value


def _load_templates() -> list:
    try:
        data = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    templates = data.get("templates") if isinstance(data, dict) else None
    return templates if isinstance(templates, list) else []


def _save_templates(templates: list) -> None:
    TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TEMPLATES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"templates": templates}, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, TEMPLATES_FILE)


@app.get("/api/templates")
async def list_templates(project: str, request: Request):
    if request.state.role not in BUILD_ROLES:
        raise HTTPException(status_code=403, detail="Template management requires operator or admin access")
    if project not in ALLOWED_PROJECTS:
        raise HTTPException(status_code=400, detail="Project is not in the allowed list")
    with _templates_lock:
        templates = _load_templates()
    result = [t for t in templates if isinstance(t, dict) and t.get("project") == project]
    return {"templates": result}


@app.post("/api/templates")
async def create_template(payload: CreateTemplateRequest, request: Request):
    if request.state.role not in BUILD_ROLES:
        raise HTTPException(status_code=403, detail="Template management requires operator or admin access")

    project = payload.project.strip()
    name = payload.name.strip()

    if not project:
        raise HTTPException(status_code=400, detail="Project is required")
    if project not in ALLOWED_PROJECTS:
        raise HTTPException(status_code=400, detail="Project is not in the allowed list")
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="Name must be 1-100 characters")
    if any(ord(c) < 32 for c in name):
        raise HTTPException(status_code=400, detail="Name contains invalid control characters")

    _validate_template_text(payload.spec.goal, "Goal", min_len=1)
    _validate_template_text(payload.spec.acceptance, "Acceptance Criteria", min_len=1)
    _validate_template_text(payload.spec.scope, "Scope")
    _validate_template_text(payload.spec.exclusions, "Exclusions")
    _validate_template_text(payload.spec.required_tests, "Required Tests")
    _validate_template_text(payload.spec.notes, "Notes")

    now = datetime.now(timezone.utc).isoformat()

    with _templates_lock:
        templates = _load_templates()
        template_id = uuid.uuid4().hex
        template = {
            "id": template_id,
            "project": project,
            "name": name,
            "spec": payload.spec.model_dump(),
            "created_at": now,
            "updated_at": now,
        }
        templates.append(template)
        _save_templates(templates)
        return template


@app.delete("/api/templates/{template_id}")
async def delete_template(template_id: str, project: str, request: Request):
    if request.state.role not in BUILD_ROLES:
        raise HTTPException(status_code=403, detail="Template management requires operator or admin access")

    if project not in ALLOWED_PROJECTS:
        raise HTTPException(status_code=400, detail="Project is not in the allowed list")

    if not re.fullmatch(r"[0-9a-f]{32}", template_id):
        raise HTTPException(status_code=404, detail="Template not found")

    with _templates_lock:
        templates = _load_templates()
        before = len(templates)
        templates = [t for t in templates if not (isinstance(t, dict) and t.get("id") == template_id and t.get("project") == project)]
        if len(templates) == before:
            raise HTTPException(status_code=404, detail="Template not found")
        _save_templates(templates)

    return {"ok": True}


# --- Cluster Health: check functions ---


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _check_http(name: str, url: str) -> dict:
    """Check an HTTP service. Returns a fixed-shape health dict."""
    now = _now_iso()
    normalized = _normalize_health_url(url)
    if normalized is None:
        detail = "unconfigured" if not url else "invalid_url"
        return {"name": name, "status": "unknown", "latency_ms": None, "last_checked": now, "detail": detail}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(f"{normalized}/health")
        latency = int((time.monotonic() - start) * 1000)
        if 200 <= response.status_code < 300:
            return {"name": name, "status": "healthy", "latency_ms": latency, "last_checked": now, "detail": None}
        elif response.status_code == 401:
            return {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "auth_failed"}
        elif response.status_code >= 500:
            return {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "http_5xx"}
        else:
            return {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "http_4xx"}
    except httpx.TimeoutException:
        return {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "timeout"}
    except httpx.ConnectError:
        return {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "connection_refused"}
    except httpx.HTTPError:
        return {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "connection_refused"}
    except Exception:
        return {"name": name, "status": "unknown", "latency_ms": None, "last_checked": now, "detail": None}


async def _check_gateway(name: str) -> tuple[dict, dict | None]:
    """Check the agent gateway. Returns (health_dict, raw_dashboard_data_or_None)."""
    now = _now_iso()
    normalized = _normalize_health_url(GATEWAY_URL)
    if normalized is None:
        detail = "unconfigured" if not GATEWAY_URL else "invalid_url"
        return (
            {"name": name, "status": "unknown", "latency_ms": None, "last_checked": now, "detail": detail},
            None,
        )
    try:
        key = GATEWAY_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return (
            {"name": name, "status": "degraded", "latency_ms": None, "last_checked": now, "detail": "auth_failed"},
            None,
        )
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(
                f"{normalized}/v1/dashboard",
                headers={"Authorization": f"Bearer {key}"},
            )
        latency = int((time.monotonic() - start) * 1000)
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except Exception:
                data = None
            return (
                {"name": name, "status": "healthy", "latency_ms": latency, "last_checked": now, "detail": None},
                data if isinstance(data, dict) else None,
            )
        elif response.status_code == 401:
            return (
                {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "auth_failed"},
                None,
            )
        elif response.status_code >= 500:
            return (
                {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "http_5xx"},
                None,
            )
        else:
            return (
                {"name": name, "status": "degraded", "latency_ms": latency, "last_checked": now, "detail": "http_4xx"},
                None,
            )
    except httpx.TimeoutException:
        return (
            {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "timeout"},
            None,
        )
    except httpx.ConnectError:
        return (
            {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "connection_refused"},
            None,
        )
    except httpx.HTTPError:
        return (
            {"name": name, "status": "offline", "latency_ms": None, "last_checked": now, "detail": "connection_refused"},
            None,
        )
    except Exception:
        return (
            {"name": name, "status": "unknown", "latency_ms": None, "last_checked": now, "detail": None},
            None,
        )


async def _check_self(name: str) -> dict:
    """The dashboard is healthy if this endpoint is responding."""
    return {"name": name, "status": "healthy", "latency_ms": 0, "last_checked": _now_iso(), "detail": None}


async def _check_lm_studio_models() -> dict:
    """Check LM Studio model availability with authentication.

    Uses the file-backed bearer token (never an environment value). The token
    is sent upstream only and never appears in any response. The authoritative
    endpoint is the native LM Studio GET /api/v1/models (not the OpenAI-compatible
    /v1/models or a nonexistent /health).
    """
    url = LMSTUDIO_URL
    now = _now_iso()
    normalized = _normalize_health_url(url)
    if normalized is None:
        detail = "unconfigured" if not url else "invalid_url"
        return {"status": "unknown", "models": [], "last_checked": now, "detail": detail, "latency_ms": None}

    # Read the bearer token from the file-backed secret.
    try:
        token = LMSTUDIO_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return {"status": "degraded", "models": [], "last_checked": now, "detail": "auth_unavailable", "latency_ms": None}

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(f"{normalized}/api/v1/models", headers=headers)
        latency = int((time.monotonic() - start) * 1000)
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except Exception:
                data = {}
            models_raw = data.get("models", []) if isinstance(data, dict) else []
            models = []
            for m in models_raw:
                if not isinstance(m, dict):
                    continue
                model_id = _sanitize_model_id(m.get("key"))
                if model_id is None:
                    continue
                loaded_instances = m.get("loaded_instances")
                loaded = isinstance(loaded_instances, list) and len(loaded_instances) > 0
                models.append({"id": model_id, "loaded": loaded})
            return {"status": "healthy", "models": models, "last_checked": now, "detail": None, "latency_ms": latency}
        elif response.status_code == 401:
            return {"status": "degraded", "models": [], "last_checked": now, "detail": "auth_failed", "latency_ms": latency}
        elif response.status_code >= 500:
            return {"status": "degraded", "models": [], "last_checked": now, "detail": "http_5xx", "latency_ms": latency}
        else:
            return {"status": "degraded", "models": [], "last_checked": now, "detail": "http_4xx", "latency_ms": latency}
    except httpx.TimeoutException:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "timeout", "latency_ms": None}
    except httpx.ConnectError:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "connection_refused", "latency_ms": None}
    except httpx.HTTPError:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "connection_refused", "latency_ms": None}
    except Exception:
        return {"status": "unknown", "models": [], "last_checked": now, "detail": None, "latency_ms": None}


def _project_running_job(job: dict) -> dict:
    """Project a single running job to the strict read-only allow-list.

    Only safe, read-only fields are included. Prompts, secrets, credentials,
    internal URLs, and filesystem/worktree paths are never passed through.
    """
    return {
        "id": job.get("workflow_id") if isinstance(job.get("workflow_id"), str) else None,
        "project": job.get("project") if isinstance(job.get("project"), str) else None,
        "stage": job.get("stage") if isinstance(job.get("stage"), str) else None,
        "model": job.get("model") if isinstance(job.get("model"), str) else None,
        "model_reason": _safe_model_reason(job.get("model_reason")),
    }


def _project_agent_queue(gateway_data: dict) -> dict:
    """Project gateway dashboard data to a safe agent queue summary.

    Reports the accurate ``running`` count and every running job (safely
    projected, bounded to ``MAX_RUNNING_JOBS``) rather than only the first, so
    concurrent jobs are not hidden.
    """
    now = _now_iso()
    if not gateway_data:
        return {
            "queued": 0, "running": 0, "running_jobs": [],
            "current_job": None, "last_checked": now, "detail": "unavailable",
        }
    jobs = gateway_data.get("jobs", [])
    if not isinstance(jobs, list):
        jobs = []
    queued = sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "queued")
    running_jobs = [
        _project_running_job(j)
        for j in jobs
        if isinstance(j, dict) and j.get("status") == "running"
    ]
    running = len(running_jobs)
    current_job = running_jobs[0] if running_jobs else None
    return {
        "queued": queued,
        "running": running,
        "running_jobs": running_jobs[:MAX_RUNNING_JOBS],
        "current_job": current_job,
        "last_checked": now,
        "detail": None,
    }


def _compute_overall(services: list[dict]) -> str:
    """Compute overall cluster status from individual service statuses."""
    relevant = [s for s in services if s.get("status") != "unknown"]
    if not relevant:
        return "unknown"
    if all(s["status"] == "healthy" for s in relevant):
        return "healthy"
    if all(s["status"] == "offline" for s in relevant):
        return "offline"
    return "degraded"


async def _lm_studio_to_service(name: str, result: dict) -> dict:
    """Map the shared LM Studio check result to a standard service health dict."""
    return {
        "name": name,
        "status": result["status"],
        "latency_ms": result.get("latency_ms"),
        "last_checked": result["last_checked"],
        "detail": result["detail"],
    }


@app.get("/api/cluster-health")
async def cluster_health(request: Request):
    """Return a fixed allow-listed safe health payload for all registered services.

    All authenticated roles (viewer, operator, admin) can read this endpoint.
    Uses bounded concurrency and strict timeouts so a down dependency cannot
    stall the dashboard.
    """
    semaphore = asyncio.Semaphore(HEALTH_CONCURRENCY)
    gateway_data: dict = {}

    async def bounded(coro):
        async with semaphore:
            return await coro

    async def check_gateway():
        nonlocal gateway_data
        result, data = await _check_gateway("agent-gateway")
        if data is not None:
            gateway_data = data
        return result

    # LM Studio models check (shared by m5-inference and lm-studio entries;
    # single authenticated probe to avoid duplicate/misleading checks)
    lm_studio_result = await bounded(_check_lm_studio_models())

    # Build all service check coroutines
    service_coros = []
    for service in HEALTH_SERVICES:
        if service["check_type"] == "http":
            service_coros.append(bounded(_check_http(service["name"], _service_url(service))))
        elif service["check_type"] == "gateway":
            service_coros.append(bounded(check_gateway()))
        elif service["check_type"] == "self":
            service_coros.append(bounded(_check_self(service["name"])))
        elif service["check_type"] == "lm_studio":
            service_coros.append(_lm_studio_to_service(service["name"], lm_studio_result))

    results = await asyncio.gather(*service_coros, return_exceptions=True)

    # Process service results
    services = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            services.append({
                "name": HEALTH_SERVICES[i]["name"],
                "status": "unknown",
                "latency_ms": None,
                "last_checked": _now_iso(),
                "detail": None,
            })
        else:
            services.append(result)

    # LM Studio detail section (from the shared authenticated result)
    lm_studio = {
        "status": lm_studio_result["status"],
        "models": lm_studio_result["models"],
        "last_checked": lm_studio_result["last_checked"],
        "detail": lm_studio_result["detail"],
    }

    # Agent queue from gateway data
    agent_queue = _project_agent_queue(gateway_data)

    # Compute overall status
    overall = _compute_overall(services)

    return {
        "generated_at": _now_iso(),
        "overall": overall,
        "services": services,
        "lm_studio": lm_studio,
        "agent_queue": agent_queue,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


class AcknowledgeAlertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_note: str
    confirm: str


_ALERT_ID_RE = re.compile(r"[0-9a-f]{12}")


def _project_active_alert(raw: dict) -> dict:
    """Project a single active alert to the strict read-only allow-list."""
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or _ALERT_ID_RE.fullmatch(job_id) is None:
        job_id = None
    return {
        "job_id": job_id,
        "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
        "role": raw.get("role") if isinstance(raw.get("role"), str) else None,
        "project": raw.get("project") if isinstance(raw.get("project"), str) else None,
        "stage": raw.get("stage") if isinstance(raw.get("stage"), str) else None,
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
    }


def _project_acknowledged_alert(raw: dict) -> dict:
    """Project a single acknowledged (history) alert to the strict read-only allow-list."""
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or _ALERT_ID_RE.fullmatch(job_id) is None:
        job_id = None
    actor = raw.get("actor")
    if not isinstance(actor, str) or len(actor) < 1 or len(actor) > 128 or any(ord(c) < 32 for c in actor):
        actor = None
    return {
        "job_id": job_id,
        "status": raw.get("status") if isinstance(raw.get("status"), str) else None,
        "role": raw.get("role") if isinstance(raw.get("role"), str) else None,
        "project": raw.get("project") if isinstance(raw.get("project"), str) else None,
        "stage": raw.get("stage") if isinstance(raw.get("stage"), str) else None,
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
        "acknowledged_at": raw.get("acknowledged_at") if isinstance(raw.get("acknowledged_at"), str) else None,
        "actor": actor,
    }


MAX_ALERT_HISTORY = 20


def _project_alerts(gateway_data: dict) -> dict:
    """Project gateway alert data to a safe alerts summary.

    Reports active alerts and recent acknowledged-alert history using only the
    infrastructure gateway safe projections. Never exposes full prompts, outputs,
    errors, filesystem paths, branches, commit SHAs, internal service URLs,
    database details, credentials, or resolution notes.
    """
    active_raw = gateway_data.get("active")
    if not isinstance(active_raw, list):
        active_raw = []
    history_raw = gateway_data.get("acknowledged")
    if not isinstance(history_raw, list):
        history_raw = []
    active = [_project_active_alert(a) for a in active_raw if isinstance(a, dict)]
    active = [a for a in active if a["job_id"] is not None]
    history = [_project_acknowledged_alert(a) for a in history_raw if isinstance(a, dict)]
    history = [a for a in history if a["job_id"] is not None]
    return {
        "active": active,
        "history": history[:MAX_ALERT_HISTORY],
    }


@app.get("/api/alerts")
async def list_alerts(request: Request):
    """Return safe projections of active alerts and acknowledgment history.

    All authenticated roles (viewer, operator, admin) can read this endpoint.
    """
    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(
                f"{GATEWAY_URL}/v1/alerts",
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Upstream gateway unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Upstream gateway error")
    try:
        raw = response.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid upstream response")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="Invalid upstream response")
    return _project_alerts(raw)


ACKNOWLEDGE_MAX_BODY_BYTES = 1024
ACKNOWLEDGE_TIMEOUT = 30.0

_ACK_PATH_RE = re.compile(r"^/api/alerts/[0-9a-f]{12}/acknowledge$")

_413_BODY = b'{"detail":"Request body too large"}'
_400_CL_BODY = b'{"detail":"Invalid Content-Length"}'


class _AcknowledgeBodyLimitMiddleware:
    """Narrowly scoped ASGI middleware enforcing a body size limit on
    POST /api/alerts/{canonical-12-hex}/acknowledge.

    Runs before routing and before Pydantic body parsing. It:
    - Early-rejects a valid Content-Length over the limit (413).
    - Rejects invalid or negative Content-Length (400).
    - Independently streams at most limit+1 bytes so missing, understated,
      or dishonest Content-Length cannot permit an oversized body.
    - Restores the bounded body for downstream parsing.
    - Returns a fixed small response with no body echo, secret, URL, path,
      or upstream data.

    Behavior is isolated to this one route; all other requests pass through
    unchanged. Authentication and security behavior is preserved.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        if not _ACK_PATH_RE.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        # --- Content-Length fast-path checks ---
        cl_raw = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"content-length":
                cl_raw = v.decode("ascii", errors="replace")
                break

        if cl_raw is not None:
            try:
                cl = int(cl_raw)
            except (ValueError, TypeError):
                await self._fixed_response(send, 400, _400_CL_BODY)
                return
            if cl < 0:
                await self._fixed_response(send, 400, _400_CL_BODY)
                return
            if cl > ACKNOWLEDGE_MAX_BODY_BYTES:
                await self._fixed_response(send, 413, _413_BODY)
                return

        # --- Stream body, reading at most limit+1 bytes ---
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if len(body) > ACKNOWLEDGE_MAX_BODY_BYTES:
                await self._fixed_response(send, 413, _413_BODY)
                return
            if not message.get("more_body", False):
                break

        # --- Replay bounded body for downstream ---
        body_sent = False

        async def bounded_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return msg

        await self.app(scope, bounded_receive, send)

    @staticmethod
    async def _fixed_response(send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_AcknowledgeBodyLimitMiddleware)


@app.post("/api/alerts/{job_id}/acknowledge")
async def acknowledge_alert(job_id: str, payload: AcknowledgeAlertRequest, request: Request):
    """Acknowledge a single active alert. Admin-only.

    Trust boundary: the agent gateway credential is cluster-wide and remains
    server-side. FirstProject intentionally exposes only this one fixed
    acknowledgment operation to the dashboard. The gateway key is read from a
    file-backed secret, sent only as an upstream Bearer token, and never
    appears in any response, log, or browser-visible artifact.

    This is the only state-changing operation exposed by the dashboard for
    alerts. It submits a fixed acknowledgment to the gateway's fixed
    acknowledgment endpoint (POST /v1/alerts/acknowledge). It never retries,
    cancels, approves, merges, deploys, pushes, discards, deletes, cleans up,
    executes shell or Git commands, changes job or workflow status, or updates
    arbitrary infrastructure data.
    """
    if request.state.role != "admin":
        _audit(request, job_id, "acknowledge_alert", "denied")
        raise HTTPException(status_code=403, detail="Alert acknowledgment requires admin access")

    if _ALERT_ID_RE.fullmatch(job_id) is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    if payload.confirm != job_id:
        _audit(request, job_id, "acknowledge_alert", "confirmation_rejected")
        raise HTTPException(status_code=400, detail="Confirmation does not match the alert job ID")

    note = payload.resolution_note.strip()
    if len(note) < 1 or len(note) > 500:
        raise HTTPException(status_code=400, detail="Resolution note must be 1 to 500 characters")
    if any(ord(c) < 32 for c in note):
        raise HTTPException(status_code=400, detail="Resolution note contains invalid control characters")

    # Actor is always server-derived from the authenticated session identity.
    # It is never accepted from the browser.
    actor = request.state.username
    if not isinstance(actor, str) or len(actor) < 1 or len(actor) > 128 or "\n" in actor:
        raise HTTPException(status_code=500, detail="Invalid server identity")

    _audit(request, job_id, "acknowledge_alert", "attempted")

    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=ACKNOWLEDGE_TIMEOUT) as client:
        try:
            response = await client.post(
                f"{GATEWAY_URL}/v1/alerts/acknowledge",
                headers={"Authorization": f"Bearer {key}"},
                json={"job_id": job_id, "resolution_note": note, "actor": actor},
            )
        except httpx.HTTPError:
            _audit(request, job_id, "acknowledge_alert", "upstream_unavailable")
            raise HTTPException(status_code=502, detail="Upstream gateway unavailable")

    if response.status_code == 409:
        _audit(request, job_id, "acknowledge_alert", "state_rejected")
        raise HTTPException(status_code=409, detail="Alert is not in an active state")
    if response.status_code == 404:
        _audit(request, job_id, "acknowledge_alert", "not_found")
        raise HTTPException(status_code=404, detail="Alert not found")
    if response.status_code >= 400:
        _audit(request, job_id, "acknowledge_alert", "upstream_failed")
        raise HTTPException(status_code=502, detail="Upstream gateway error")
    _audit(request, job_id, "acknowledge_alert", "succeeded")
    return {"ok": True, "job_id": job_id}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
