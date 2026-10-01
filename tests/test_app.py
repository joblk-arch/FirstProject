import base64
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import httpx
import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

import app


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _make_gateway_response(status_code: int = 200, json_data=None):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    if json_data is not None:
        mock_response.json.return_value = json_data
    else:
        mock_response.json.side_effect = Exception("Invalid JSON")
    return mock_response


def _make_async_client_mock(gateway_response):
    """Create a mock for httpx.AsyncClient that returns the given response."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=gateway_response)
    mock_client.post = AsyncMock(return_value=gateway_response)
    return mock_client


@pytest.fixture
def password_file(tmp_path: Path) -> Path:
    pf = tmp_path / "password"
    pf.write_text("test-pass", encoding="utf-8")
    return pf


@pytest.fixture
def gateway_key_file(tmp_path: Path) -> Path:
    kf = tmp_path / "gateway_key"
    kf.write_text("test-gateway-key", encoding="utf-8")
    return kf


@pytest.fixture
def auth_headers() -> dict:
    return {"Authorization": _basic_auth("admin", "test-pass")}


@pytest.fixture(autouse=True)
def isolated_identity_and_audit(tmp_path: Path):
    with patch.object(app, "USERS_FILE", tmp_path / "missing-users.json"), \
         patch.object(app, "AUDIT_LOG_FILE", tmp_path / "audit.jsonl"):
        yield


# --- Existing authentication tests ---


def test_authentication_accepts_configured_admin(tmp_path: Path):
    password = tmp_path / "password"
    password.write_text("secret-value", encoding="utf-8")
    with patch.object(app, "PASSWORD_FILE", password):
        assert app.authenticate(HTTPBasicCredentials(username="admin", password="secret-value")) == "admin"


def test_authentication_rejects_wrong_password(tmp_path: Path):
    password = tmp_path / "password"
    password.write_text("secret-value", encoding="utf-8")
    with patch.object(app, "PASSWORD_FILE", password), pytest.raises(ValueError):
        app.authenticate(HTTPBasicCredentials(username="admin", password="wrong"))


# --- Workflow detail: authentication ---


def test_workflow_detail_requires_auth(password_file, gateway_key_file):
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        response = client.get("/api/workflows/abc123")
        assert response.status_code == 401


def test_workflow_detail_rejects_wrong_password(password_file, gateway_key_file):
    headers = {"Authorization": _basic_auth("admin", "wrong-pass")}
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        response = client.get("/api/workflows/abc123", headers=headers)
        assert response.status_code == 401


# --- Workflow detail: live gateway contract ---


def _gateway_payload(diff="diff --git a/file.py b/file.py\n+safe"):
    payload = {
        "workflow": {
            "id": "0123456789", "objective": "Build feature X",
            "project": "firstproject", "overall": "ready-for-approval",
            "elapsed_seconds": 42, "job_id": "must-not-pass-through",
        },
        "stages": [{
            "stage": "test", "role": "tester", "status": "completed",
            "duration_seconds": 12, "model": "local-model",
            "prompt_tokens": 100, "completion_tokens": 50,
            "total_tokens": 150, "report": "All tests passed",
            "worktree": "/must/not/pass/through",
        }],
        "tester_evidence": "All tests passed",
        "reviewer_verdict": "APPROVE",
        "prompt": "must-not-pass-through",
    }
    if diff is not None:
        payload["diff"] = diff
    return payload


def test_workflow_detail_projects_live_contract(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(200, _gateway_payload()))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get(
            "/api/workflows/0123456789", headers=auth_headers
        )
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {
        "id", "objective", "project", "status", "elapsed_seconds", "stages",
        "tester_evidence", "reviewer_verdict", "diff",
    }
    assert data["status"] == "ready-for-approval"
    assert data["reviewer_verdict"] == "APPROVE"
    assert set(data["stages"][0]) == {
        "stage", "role", "status", "duration_seconds", "model",
        "prompt_tokens", "completion_tokens", "total_tokens", "report",
    }
    assert "must-not-pass-through" not in str(data)
    requested = mock_client.get.await_args.args[0]
    assert requested.endswith("/v1/workflows/0123456789/result")


def test_workflow_detail_omits_absent_or_invalid_diff(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload(diff=None)
    payload["diff"] = {"unsafe": True}
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get(
            "/api/workflows/0123456789", headers=auth_headers
        )
    assert "diff" not in response.json()


def test_workflow_detail_rejects_invalid_identifier(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).get(
            "/api/workflows/not-valid", headers=auth_headers
        )
    assert response.status_code == 404


# --- Workflow detail: not-found ---


def test_workflow_detail_not_found(password_file, gateway_key_file, auth_headers):
    gateway_response = _make_gateway_response(404)
    mock_client = _make_async_client_mock(gateway_response)

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app.app)
        response = client.get("/api/workflows/0123456789", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow not found"


# --- Workflow detail: upstream failure ---


def test_workflow_detail_upstream_500(password_file, gateway_key_file, auth_headers):
    gateway_response = _make_gateway_response(500)
    mock_client = _make_async_client_mock(gateway_response)

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app.app)
        response = client.get("/api/workflows/0123456789", headers=auth_headers)

    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway error"


def test_workflow_detail_upstream_connection_error(password_file, gateway_key_file, auth_headers):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app.app)
        response = client.get("/api/workflows/0123456789", headers=auth_headers)

    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"


def test_workflow_action_proxies_allowlisted_action(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows/0123456789/actions/approve",
            headers=auth_headers,
            json={"confirm": "0123456789"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "action": "approve"}
    call = mock_client.post.await_args
    assert call.args[0].endswith("/v1/workflows/0123456789/actions/approve")
    assert call.kwargs["json"] == {"confirm": "0123456789"}


def test_workflow_action_rejects_unknown_action_or_confirmation(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        unknown = client.post(
            "/api/workflows/0123456789/actions/delete",
            headers=auth_headers,
            json={"confirm": "0123456789"},
        )
        mismatch = client.post(
            "/api/workflows/0123456789/actions/approve",
            headers=auth_headers,
            json={"confirm": "aaaaaaaaaa"},
        )
    assert unknown.status_code == 404
    assert mismatch.status_code == 400


def test_workflow_action_maps_gateway_conflict(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(409, {"error": "unsafe detail"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows/0123456789/actions/retry",
            headers=auth_headers,
            json={"confirm": "0123456789"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Action is not valid for the workflow's current state"
    assert "unsafe" not in response.text


def _user_record(password: str, role: str) -> dict:
    salt = b"0123456789abcdef"
    iterations = 100_000
    digest = app.hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {
        "role": role,
        "salt": salt.hex(),
        "password_hash": digest.hex(),
        "iterations": iterations,
    }


def test_named_viewer_session_and_admin_action_denial(tmp_path: Path):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        app.json.dumps({"reader": _user_record("viewer-password", "viewer")}),
        encoding="utf-8",
    )
    audit_file = tmp_path / "audit.jsonl"
    headers = {"Authorization": _basic_auth("reader", "viewer-password")}
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file):
        client = TestClient(app.app)
        session = client.get("/api/session", headers=headers)
        denied = client.post(
            "/api/workflows/0123456789/actions/approve",
            headers=headers,
            json={"confirm": "0123456789"},
        )
    assert session.json() == {"username": "reader", "role": "viewer"}
    assert denied.status_code == 403
    audit = app.json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit["actor"] == "reader"
    assert audit["role"] == "viewer"
    assert audit["action"] == "approve"
    assert audit["outcome"] == "denied"


def test_successful_action_appends_attempt_and_success_audit(
    password_file, gateway_key_file, auth_headers, tmp_path: Path
):
    audit_file = tmp_path / "action-audit.jsonl"
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows/0123456789/actions/approve",
            headers=auth_headers,
            json={"confirm": "0123456789"},
        )
    assert response.status_code == 200
    entries = [app.json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [entry["outcome"] for entry in entries] == ["attempted", "succeeded"]
    assert all(entry["actor"] == "admin" for entry in entries)
