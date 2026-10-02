import hmac
import base64
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

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

# --- Cluster Health configuration ---

HEALTH_TIMEOUT = float(os.getenv("HEALTH_CHECK_TIMEOUT", "5"))
HEALTH_CONCURRENCY = int(os.getenv("HEALTH_CHECK_CONCURRENCY", "8"))

HEALTH_SERVICES: list[dict] = [
    {"name": "m5-inference", "url_env": "M5_HOST_URL", "check_type": "http"},
    {"name": "lm-studio", "url_env": "LMSTUDIO_URL", "check_type": "http"},
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


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    try:
        scheme, value = request.headers.get("Authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            raise ValueError
        username, password = base64.b64decode(value).decode("utf-8").split(":", 1)
        username, role = authenticate_identity(
            HTTPBasicCredentials(username=username, password=password)
        )
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Local AI Operations"'})
    request.state.username = username
    request.state.role = role
    return await call_next(request)


@app.get("/api/session")
async def session(request: Request):
    return {"username": request.state.username, "role": request.state.role}


@app.get("/api/dashboard")
async def dashboard():
    key = _read_gateway_key()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{GATEWAY_URL}/v1/dashboard",
            headers={"Authorization": f"Bearer {key}"},
        )
        response.raise_for_status()
        return response.json()


def _project_stage(raw: dict) -> dict:
    """Project a single stage to the strict read-only allow-list."""
    return {
        "stage": raw.get("stage"),
        "role": raw.get("role"),
        "status": raw.get("status"),
        "duration_seconds": raw.get("duration_seconds"),
        "model": raw.get("model"),
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

    result: dict = {
        "id": workflow_raw.get("id"),
        "objective": workflow_raw.get("objective"),
        "project": workflow_raw.get("project"),
        "status": workflow_raw.get("overall"),
        "elapsed_seconds": workflow_raw.get("elapsed_seconds"),
        "stages": [
            _project_stage(s) for s in stages_raw if isinstance(s, dict)
        ],
        "tester_evidence": raw.get("tester_evidence")
        if isinstance(raw.get("tester_evidence"), str) else None,
        "reviewer_verdict": raw.get("reviewer_verdict")
        if raw.get("reviewer_verdict") in {"APPROVE", "REJECT"} else None,
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
    """Check LM Studio model availability. Projects only sanitized id and loaded fields."""
    url = LMSTUDIO_URL
    now = _now_iso()
    normalized = _normalize_health_url(url)
    if normalized is None:
        detail = "unconfigured" if not url else "invalid_url"
        return {"status": "unknown", "models": [], "last_checked": now, "detail": detail}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
            response = await client.get(f"{normalized}/v1/models")
        latency = int((time.monotonic() - start) * 1000)
        if 200 <= response.status_code < 300:
            try:
                data = response.json()
            except Exception:
                data = {}
            models_raw = data.get("data", []) if isinstance(data, dict) else []
            models = []
            for m in models_raw:
                if not isinstance(m, dict):
                    continue
                model_id = _sanitize_model_id(m.get("id"))
                if model_id is None:
                    continue
                models.append({"id": model_id, "loaded": bool(m.get("loaded", False))})
            return {"status": "healthy", "models": models, "last_checked": now, "detail": None}
        elif response.status_code >= 500:
            return {"status": "degraded", "models": [], "last_checked": now, "detail": "http_5xx"}
        else:
            return {"status": "degraded", "models": [], "last_checked": now, "detail": "http_4xx"}
    except httpx.TimeoutException:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "timeout"}
    except httpx.ConnectError:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "connection_refused"}
    except httpx.HTTPError:
        return {"status": "offline", "models": [], "last_checked": now, "detail": "connection_refused"}
    except Exception:
        return {"status": "unknown", "models": [], "last_checked": now, "detail": None}


def _project_running_job(job: dict) -> dict:
    """Project a single running job to the strict read-only allow-list.

    Only safe, read-only fields are included. Prompts, secrets, credentials,
    internal URLs, and filesystem/worktree paths are never passed through.
    """
    return {
        "id": job.get("workflow_id") if isinstance(job.get("workflow_id"), str) else None,
        "project": job.get("project") if isinstance(job.get("project"), str) else None,
        "stage": job.get("stage") if isinstance(job.get("stage"), str) else None,
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

    # Build all service check coroutines
    service_coros = []
    for service in HEALTH_SERVICES:
        if service["check_type"] == "http":
            service_coros.append(bounded(_check_http(service["name"], _service_url(service))))
        elif service["check_type"] == "gateway":
            service_coros.append(bounded(check_gateway()))
        elif service["check_type"] == "self":
            service_coros.append(bounded(_check_self(service["name"])))

    # LM Studio models check (additional detail beyond the general health check)
    lm_studio_coro = bounded(_check_lm_studio_models())

    all_coros = service_coros + [lm_studio_coro]
    results = await asyncio.gather(*all_coros, return_exceptions=True)

    # Process service results
    services = []
    for i, result in enumerate(results[: len(HEALTH_SERVICES)]):
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

    # Process LM Studio models result
    lm_result = results[len(HEALTH_SERVICES)]
    if isinstance(lm_result, Exception):
        lm_studio = {"status": "unknown", "models": [], "last_checked": _now_iso(), "detail": None}
    else:
        lm_studio = lm_result

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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
