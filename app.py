import hmac
import base64
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response


GATEWAY_URL = os.getenv("AGENT_GATEWAY_URL", "http://host.docker.internal:8765").rstrip("/")
GATEWAY_KEY_FILE = Path(os.getenv("AGENT_GATEWAY_KEY_FILE", "/run/secrets/agent_gateway_key"))
PASSWORD_FILE = Path(os.getenv("DASHBOARD_PASSWORD_FILE", "/run/secrets/dashboard_password"))
USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")

app = FastAPI(title="Local AI Operations")
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials) -> str:
    expected_password = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    valid = hmac.compare_digest(credentials.username, USERNAME) and hmac.compare_digest(
        credentials.password, expected_password
    )
    if not valid:
        raise ValueError("Invalid credentials")
    return credentials.username


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    try:
        scheme, value = request.headers.get("Authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            raise ValueError
        username, password = base64.b64decode(value).decode("utf-8").split(":", 1)
        authenticate(HTTPBasicCredentials(username=username, password=password))
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Local AI Operations"'})
    return await call_next(request)


@app.get("/api/dashboard")
async def dashboard():
    key = GATEWAY_KEY_FILE.read_text(encoding="utf-8").strip()
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


@app.get("/api/workflows/{workflow_id}")
async def workflow_detail(workflow_id: str):
    if re.fullmatch(r"[0-9a-f]{10}", workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    key = GATEWAY_KEY_FILE.read_text(encoding="utf-8").strip()
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


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
