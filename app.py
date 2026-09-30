import hmac
import base64
import os
from pathlib import Path

import httpx
from fastapi import FastAPI
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


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
