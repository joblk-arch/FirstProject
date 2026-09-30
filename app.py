import hmac
import os
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles


GATEWAY_URL = os.getenv("AGENT_GATEWAY_URL", "http://host.docker.internal:8765").rstrip("/")
GATEWAY_KEY_FILE = Path(os.getenv("AGENT_GATEWAY_KEY_FILE", "/run/secrets/agent_gateway_key"))
PASSWORD_FILE = Path(os.getenv("DASHBOARD_PASSWORD_FILE", "/run/secrets/dashboard_password"))
USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")

app = FastAPI(title="Local AI Operations")
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_password = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    valid = hmac.compare_digest(credentials.username, USERNAME) and hmac.compare_digest(
        credentials.password, expected_password
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/api/dashboard")
async def dashboard(_user: str = Depends(authenticate)):
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
