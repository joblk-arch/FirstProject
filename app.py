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


GATEWAY_URL = os.getenv("AGENT_GATEWAY_URL", "http://host.docker.internal:8765").rstrip("/")
GATEWAY_KEY_FILE = Path(os.getenv("AGENT_GATEWAY_KEY_FILE", "/run/secrets/agent_gateway_key"))
PASSWORD_FILE = Path(os.getenv("DASHBOARD_PASSWORD_FILE", "/run/secrets/dashboard_password"))
USERS_FILE = Path(os.getenv("DASHBOARD_USERS_FILE", "/run/secrets/dashboard_users"))
AUDIT_LOG_FILE = Path(os.getenv("DASHBOARD_AUDIT_LOG_FILE", "/tmp/local-ai-dashboard-audit.jsonl"))
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


class StartWorkflowRequest(BaseModel):
    project: str
    objective: str
    reasoning: str = "standard"
    idempotency_key: str

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


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
