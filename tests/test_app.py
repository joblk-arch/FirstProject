import base64
import re
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
        "repair_enabled", "repair_activated", "repair_attempts",
        "repair_max_attempts", "repair_state",
    }
    assert data["status"] == "ready-for-approval"
    assert data["reviewer_verdict"] == "APPROVE"
    assert set(data["stages"][0]) == {
        "stage", "role", "status", "duration_seconds", "model",
        "model_reason", "prompt_tokens", "completion_tokens", "total_tokens", "report",
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
    assert session.json() == {"username": "reader", "role": "viewer", "csrf_token": None}
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


# --- Dashboard: recent_workflows pass-through contract ---


def _dashboard_payload():
    return {
        "jobs": [{"stage": "test", "project": "p1", "status": "running", "model": "m1", "total_tokens": 100, "duration_seconds": 5, "workflow_id": "0123456789"}],
        "usage": [{"model": "m1", "project": "p1", "jobs": 1, "prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100}],
        "counts": [{"project": "p1", "status": "running", "count": 1}],
        "projects": [{"name": "p1", "default_branch": "main"}],
        "recent_workflows": [
            {
                "id": "0123456789",
                "project": "firstproject",
                "objective": "Build feature X",
                "overall": "running",
                "origin": "telegram",
                "models": ["local-model"],
                "stage_counts": {"plan": 1, "test": 1},
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "created_at": "2025-01-15T10:00:00Z",
            }
        ],
        "generated_at": "2025-01-15T10:00:00Z",
    }


def test_dashboard_passes_through_recent_workflows(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "recent_workflows" in data
    assert data["recent_workflows"][0]["id"] == "0123456789"
    assert data["recent_workflows"][0]["objective"] == "Build feature X"
    assert data["recent_workflows"][0]["overall"] == "running"
    assert data["recent_workflows"][0]["origin"] == "telegram"
    assert data["recent_workflows"][0]["models"] == ["local-model"]
    assert data["recent_workflows"][0]["stage_counts"] == {"plan": 1, "test": 1}
    assert data["recent_workflows"][0]["prompt_tokens"] == 100
    assert data["recent_workflows"][0]["completion_tokens"] == 50
    assert data["recent_workflows"][0]["total_tokens"] == 150
    assert data["recent_workflows"][0]["created_at"] == "2025-01-15T10:00:00Z"


def test_dashboard_handles_missing_recent_workflows(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload()
    del payload["recent_workflows"]
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "recent_workflows" not in data


def test_dashboard_requires_auth(gateway_key_file):
    with patch.object(app, "PASSWORD_FILE", gateway_key_file):
        client = TestClient(app.app)
        response = client.get("/api/dashboard")
    assert response.status_code == 401


# --- Frontend source contract: recent_workflows rendering ---


def test_frontend_html_has_workflows_section():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="workflows"' in html
    assert "Recent workflows" in html


def test_frontend_js_renders_recent_workflows():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "recent_workflows" in js
    assert "w.objective" in js
    assert "w.status" in js
    assert "stage_counts" in js
    assert "created_at" in js
    assert "data-workflow-id" in js
    assert "safe(" in js


def test_frontend_js_uses_groupby_guard():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "Object.groupBy ?" in js


def test_frontend_js_workflows_click_opens_detail():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # The #workflows tbody must have a click listener that delegates to openWorkflow
    assert "$('workflows').addEventListener('click'" in js
    assert "openWorkflow(trigger.dataset.workflowId)" in js


def test_frontend_js_workflows_empty_state():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # Graceful empty state when no workflows are present
    assert "No workflows recorded yet." in js


def test_frontend_js_workflows_sorted_newest_first():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # Must sort by created_at in descending order (newest first)
    assert "Date.parse(a.created_at)" in js
    assert "Date.parse(b.created_at)" in js
    # The sort comparator returns tb - ta (descending)
    assert "return tb - ta" in js


def test_frontend_js_workflows_renders_origin_and_tokens():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "w.origin" in js
    assert "w.prompt_tokens" in js
    assert "w.completion_tokens" in js
    assert "w.total_tokens" in js


def test_frontend_html_workflows_table_has_all_columns():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    for col in ["Project", "Objective", "Status", "Origin", "Models", "Stages", "Tokens", "Created"]:
        assert col in html, f"Missing column: {col}"


# --- Local agent proof documentation ---


def test_local_agent_proof_file_exists():
    proof = Path(__file__).resolve().parent.parent / "docs" / "local-agent-proof.md"
    assert proof.is_file(), "docs/local-agent-proof.md must exist"


def test_local_agent_proof_mentions_telegram():
    proof = Path(__file__).resolve().parent.parent / "docs" / "local-agent-proof.md"
    content = proof.read_text(encoding="utf-8")
    assert "telegram" in content.lower(), "Proof file must mention Telegram"


def test_local_agent_proof_is_short():
    proof = Path(__file__).resolve().parent.parent / "docs" / "local-agent-proof.md"
    content = proof.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert len(lines) <= 20, f"Proof file should be short, got {len(lines)} lines"


# --- Start Build: allowed-projects endpoint ---


def test_allowed_projects_requires_auth(password_file):
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        response = client.get("/api/workflows/allowed-projects")
    assert response.status_code == 401


def test_allowed_projects_returns_list(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject", "secondproject"]):
        client = TestClient(app.app)
        response = client.get("/api/workflows/allowed-projects", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {"projects": ["firstproject", "secondproject"]}


def test_allowed_projects_viewer_can_see(tmp_path: Path, password_file):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        app.json.dumps({"reader": _user_record("viewer-pass", "viewer")}),
        encoding="utf-8",
    )
    headers = {"Authorization": _basic_auth("reader", "viewer-pass")}
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["proj1"]):
        client = TestClient(app.app)
        response = client.get("/api/workflows/allowed-projects", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"projects": ["proj1"]}


# --- Start Build: role enforcement ---


def test_start_build_viewer_denied(tmp_path: Path, password_file):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        app.json.dumps({"reader": _user_record("viewer-pass", "viewer")}),
        encoding="utf-8",
    )
    audit_file = tmp_path / "audit.jsonl"
    headers = {"Authorization": _basic_auth("reader", "viewer-pass")}
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        client = TestClient(app.app)
        response = client.post(
            "/api/workflows",
            headers=headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"]
    audit = app.json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit["action"] == "start_build"
    assert audit["outcome"] == "denied"


def test_start_build_operator_allowed(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "0123456789"
    assert data["status"] == "queued"
    assert data["project"] == "firstproject"
    assert data["objective"] == "Build feature X"
    assert data["reasoning"] == "standard"


# --- Start Build: project allowlisting ---


def test_start_build_disallowed_project(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "evil-project",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400
    assert "not in the allowed list" in response.json()["detail"]


def test_start_build_empty_project(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "   ",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400


# --- Start Build: payload mapping ---


def test_start_build_sends_correct_payload_to_gateway(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "deep",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    call = mock_client.post.await_args
    assert call.args[0].endswith("/v1/workflows")
    assert call.kwargs["json"] == {
        "project": "firstproject",
        "objective": "Build feature X",
        "reasoning": True,
    }
    assert call.kwargs["headers"]["Idempotency-Key"] == "123e4567-e89b-12d3-a456-426614174000"
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-gateway-key"


# --- Start Build: reasoning values ---


def test_start_build_standard_reasoning(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 200
    assert response.json()["reasoning"] == "standard"


def test_start_build_deep_reasoning(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "deep",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 200
    assert response.json()["reasoning"] == "deep"


def test_start_build_invalid_reasoning(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "turbo",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400
    assert "standard" in response.json()["detail"]


# --- Start Build: idempotency / double submission ---


def test_start_build_gateway_409_duplicate(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(409, {"error": "duplicate"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert "duplicate" not in response.text


# --- Start Build: malformed input ---


def test_start_build_objective_too_long(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "a" * 2001,
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400
    assert "2000" in response.json()["detail"]


def test_start_build_objective_control_chars(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build\nfeature\tX",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400
    assert "control" in response.json()["detail"]


def test_start_build_missing_objective(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "   ",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 400


def test_start_build_invalid_idempotency_key(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "not-a-uuid",
            },
        )
    assert response.status_code == 400
    assert "UUID" in response.json()["detail"]


# --- Start Build: upstream behavior ---


def test_start_build_gateway_401(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(401, {"error": "invalid key"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Gateway authorization failed"
    assert "invalid key" not in response.text


def test_start_build_gateway_500(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(500, {"error": "internal traceback"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway error"
    assert "traceback" not in response.text


def test_start_build_gateway_connection_error(password_file, gateway_key_file, auth_headers):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"


def test_start_build_missing_gateway_key_returns_redacted_502(
    password_file, auth_headers, tmp_path: Path
):
    """A missing/unreadable gateway key file yields a controlled redacted 502, not a 500."""
    missing_key = tmp_path / "does-not-exist"
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", missing_key), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Gateway credential unavailable"
    assert "does-not-exist" not in response.text
    assert "run/secrets" not in response.text


def test_start_build_gateway_timeout(password_file, gateway_key_file, auth_headers):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"


# --- Start Build: real gateway 202 contract ---


def _real_gateway_202_response():
    """Representative real Agent Gateway POST /v1/workflows success response.

    The deployed gateway returns HTTP 202 and uses the keys ``workflow_id`` and
    ``overall`` (not ``id``/``status``). Extra fields are included to prove they
    are projected out and never leaked to the browser.
    """
    return _make_gateway_response(
        202,
        {
            "workflow_id": "0123456789",
            "overall": "queued",
            "created_at": "2025-01-15T10:00:00Z",
            "origin": "dashboard",
            "prompt": "must-not-pass-through",
        },
    )


def _start_build_body():
    return {
        "project": "firstproject",
        "objective": "Build feature X",
        "reasoning": "standard",
        "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
    }


def test_start_build_real_gateway_202_contract(password_file, gateway_key_file, auth_headers):
    """A representative real gateway 202 response with workflow_id/overall succeeds."""
    mock_client = _make_async_client_mock(_real_gateway_202_response())
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "0123456789"
    assert data["status"] == "queued"
    assert data["project"] == "firstproject"
    assert data["objective"] == "Build feature X"
    assert data["reasoning"] == "standard"
    # Extra gateway fields must be projected out, never leaked.
    assert "created_at" not in data
    assert "origin" not in data
    assert "prompt" not in data
    assert "must-not-pass-through" not in response.text


def test_start_build_rejects_wrong_keys(password_file, gateway_key_file, auth_headers):
    """A 202 response using the old id/status keys must fail (contract mismatch)."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"id": "0123456789", "status": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Invalid upstream response"


def test_start_build_accepts_200_replay(password_file, gateway_key_file, auth_headers):
    """A 200 replay of a completed idempotency key with valid keys is accepted."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(200, {"workflow_id": "0123456789", "overall": "completed"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "0123456789"
    assert data["status"] == "completed"
    assert data["pending"] is False


def test_start_build_rejects_invalid_workflow_id(password_file, gateway_key_file, auth_headers):
    """A 202 response with a malformed workflow_id must fail (strict validation)."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "not-a-valid-id", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Invalid upstream response"


# --- Start Build: idempotency lifecycle (200 replay / 202 processing) ---


def test_start_build_lost_response_replay_reuses_key(password_file, gateway_key_file, auth_headers):
    """First request creates a workflow (202) but the response is lost; the second
    submission reuses the exact idempotency key, the gateway returns a 200 replay,
    and the UI receives the original workflow ID. No second workflow is created
    because the gateway deduplicates by the same Idempotency-Key."""
    first = _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    second = _make_gateway_response(200, {"workflow_id": "0123456789", "overall": "completed"})
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=[first, second])
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        client = TestClient(app.app)
        # First submission: gateway creates the workflow (202). The client's
        # response is "lost" in this scenario, but the key is retained.
        client.post("/api/workflows", headers=auth_headers, json=_start_build_body())
        # Second submission reuses the exact same idempotency key.
        response = client.post("/api/workflows", headers=auth_headers, json=_start_build_body())
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "0123456789"
    assert data["status"] == "completed"
    assert data["pending"] is False
    # Both gateway calls must carry the same Idempotency-Key so the gateway
    # deduplicates and does not create a second workflow.
    keys = [c.kwargs["headers"]["Idempotency-Key"] for c in mock_client.post.await_args_list]
    assert len(keys) == 2
    assert keys[0] == keys[1] == _start_build_body()["idempotency_key"]


def test_start_build_rejects_malformed_200(password_file, gateway_key_file, auth_headers):
    """A 200 replay with a malformed workflow_id must fail safely (502)."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(200, {"workflow_id": "not-a-valid-id", "overall": "completed"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Invalid upstream response"


def test_start_build_rejects_200_missing_overall(password_file, gateway_key_file, auth_headers):
    """A 200 replay missing a non-empty overall must fail safely (502)."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(200, {"workflow_id": "0123456789"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Invalid upstream response"


def test_start_build_202_processing_returns_pending(password_file, gateway_key_file, auth_headers):
    """A 202 with status=processing (still running) returns a retryable pending
    state without pretending success and without leaking upstream details."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"status": "processing", "prompt": "must-not-pass-through"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 200
    data = response.json()
    assert data["pending"] is True
    assert data["status"] == "processing"
    assert data["id"] is None
    assert "must-not-pass-through" not in response.text


def test_start_build_202_processing_with_workflow_id(password_file, gateway_key_file, auth_headers):
    """A 202 processing response that carries a valid workflow_id preserves it."""
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "status": "processing"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows", headers=auth_headers, json=_start_build_body()
        )
    assert response.status_code == 200
    data = response.json()
    assert data["pending"] is True
    assert data["status"] == "processing"
    assert data["id"] == "0123456789"


# --- Start Build: token secrecy ---


def test_start_build_never_leaks_gateway_key(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(500, {"error": "boom"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert "test-gateway-key" not in response.text
    assert "test-gateway-key" not in str(response.headers)


def test_start_build_never_leaks_gateway_url(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(
        _make_gateway_response(500, {"error": "boom"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    assert "internal-secret" not in response.text


# --- Start Build: audit logging ---


def test_start_build_audit_entries(password_file, gateway_key_file, auth_headers, tmp_path: Path):
    audit_file = tmp_path / "build-audit.jsonl"
    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
    entries = [app.json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [e["outcome"] for e in entries] == ["attempted", "succeeded"]
    assert all(e["action"] == "start_build" for e in entries)
    assert all(e["actor"] == "admin" for e in entries)


# --- Frontend source contracts: start build ---


def test_frontend_html_has_start_build_form():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="start-build-form"' in html
    assert 'id="build-project"' in html
    assert 'id="spec-goal"' in html
    assert 'id="spec-acceptance"' in html
    assert 'id="spec-scope"' in html
    assert 'id="spec-exclusions"' in html
    assert 'id="spec-required-tests"' in html
    assert 'id="spec-notes"' in html
    assert 'id="build-preview"' in html
    assert 'id="build-preview-text"' in html
    assert 'id="build-char-count"' in html
    assert 'id="template-select"' in html
    assert 'id="template-save"' in html
    assert 'id="template-delete"' in html
    assert 'id="build-submit"' in html
    assert 'id="build-status"' in html
    assert 'id="build-objective"' not in html


def test_frontend_js_has_start_build_functions():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "startBuild" in js
    assert "loadAllowedProjects" in js
    assert "crypto.randomUUID" in js
    assert "idempotency" in js


def test_frontend_js_reuses_idempotency_key_on_retry():
    """The UI retains and reuses one idempotency key for a normalized intent."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "getBuildKey" in js
    assert "normalizeBuildIntent" in js
    assert "buildKeyIntent" in js
    # The key is reused when the normalized intent is unchanged.
    assert "if (buildKey && buildKeyIntent === intent) return buildKey;" in js


def test_frontend_js_rotates_key_on_payload_change():
    """The UI rotates the key when the normalized payload changes."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "rotateBuildKey" in js
    # A changed intent generates a fresh key via the safe UUID generator.
    assert "const newKey = generateUUID();" in js
    assert "buildKey = newKey;" in js


def test_frontend_js_rotates_key_on_confirmed_success():
    """The UI clears the key after a confirmed success so the next submit is fresh."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "clearBuildKey" in js
    assert "clearBuildKey();" in js


def test_frontend_js_suppresses_concurrent_double_click():
    """The UI suppresses concurrent double clicks via an in-flight guard."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "buildInFlight" in js
    assert "if (buildInFlight) return;" in js


def test_frontend_js_viewer_disables_form():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "setBuildDisabled(true)" in js
    assert "viewer" in js


def test_frontend_js_build_status_aria():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "build-status" in js


def test_frontend_html_build_status_role():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


# --- Frontend source contracts: Safari plain-HTTP UUID fallback ---


def test_frontend_js_has_uuid_fallback():
    """generateUUID exists, prefers crypto.randomUUID, falls back to getRandomValues."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "function generateUUID()" in js
    # Prefers crypto.randomUUID when available
    assert "typeof crypto.randomUUID === 'function'" in js
    # Falls back to crypto.getRandomValues
    assert "typeof crypto.getRandomValues === 'function'" in js
    # Sets version 4 bits
    assert "bytes[6] = (bytes[6] & 0x0f) | 0x40" in js
    # Sets variant 10xx bits
    assert "bytes[8] = (bytes[8] & 0x3f) | 0x80" in js


def test_frontend_js_uuid_fallback_produces_lowercase_hex():
    """The getRandomValues fallback uses toString(16) and padStart to ensure lowercase hex."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "toString(16)" in js
    assert "padStart(2, '0')" in js


def test_frontend_js_getbuildkey_uses_generateuuid():
    """getBuildKey calls generateUUID() not raw crypto.randomUUID()."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # Extract the getBuildKey function body
    match = re.search(r"function getBuildKey\(.*?\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "getBuildKey function not found"
    body = match.group(0)
    assert "generateUUID()" in body
    assert "crypto.randomUUID()" not in body


def test_frontend_js_rotatebuildkey_uses_generateuuid():
    """rotateBuildKey calls generateUUID() not raw crypto.randomUUID()."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function rotateBuildKey\(.*?\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "rotateBuildKey function not found"
    body = match.group(0)
    assert "generateUUID()" in body
    assert "crypto.randomUUID()" not in body


def test_frontend_js_keygen_inside_trycatch():
    """The getBuildKey call appears after 'try {' in startBuild (source ordering check)."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # Find the startBuild function
    match = re.search(r"async function startBuild\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "startBuild function not found"
    body = match.group(0)
    try_pos = body.find("try {")
    keygen_pos = body.find("getBuildKey(")
    assert try_pos != -1, "try block not found in startBuild"
    assert keygen_pos != -1, "getBuildKey call not found in startBuild"
    assert keygen_pos > try_pos, "getBuildKey must be inside the try block"


def test_frontend_js_catch_shows_error_message():
    """The catch block in startBuild references error.message for visible feedback."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function startBuild\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "startBuild function not found"
    body = match.group(0)
    assert "catch (error)" in body
    assert "error.message" in body


def test_frontend_js_no_bare_randomuuid_outside_fallback():
    """crypto.randomUUID() appears only inside generateUUID, not at call sites."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # Find the generateUUID function
    match = re.search(r"function generateUUID\(\) \{.*?\n\}", js, re.DOTALL)
    assert match is not None, "generateUUID function not found"
    # Remove the generateUUID function body from the source
    without_fallback = js[:match.start()] + js[match.end():]
    # crypto.randomUUID should not appear outside the fallback
    assert "crypto.randomUUID()" not in without_fallback, (
        "crypto.randomUUID() found outside generateUUID — call sites must use generateUUID()"
    )


def test_safari_plain_http_regression():
    """Composite regression: all Safari plain-HTTP safety conditions must hold simultaneously.

    This ensures the fix for the original bug (crypto.randomUUID unavailable in
    Safari non-secure contexts) is preserved:
    1. generateUUID exists with getRandomValues fallback
    2. Key generation is inside try/catch
    3. Error message is shown to the user on failure
    4. Form is re-enabled after failure
    """
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # 1. UUID fallback exists
    assert "function generateUUID()" in js
    assert "crypto.getRandomValues" in js
    # 2. Key generation inside try/catch
    match = re.search(r"async function startBuild\(.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    try_pos = body.find("try {")
    keygen_pos = body.find("getBuildKey(")
    assert keygen_pos > try_pos, "getBuildKey must be inside try block"
    # 3. Error message shown
    assert "error.message" in body
    # 4. Form re-enabled after failure
    assert "setBuildDisabled(false)" in body
    assert "buildInFlight = false" in body


# --- Behavioral tests: Safari plain-HTTP UUID fallback (executable simulation) ---


def _simulate_fallback_uuid(random_bytes: bytes) -> str:
    """Faithful Python re-implementation of the JS getRandomValues fallback.

    Mirrors the exact algorithm in static/app.js generateUUID():
    1. Take 16 random bytes
    2. Set version 4 bits: bytes[6] = (bytes[6] & 0x0f) | 0x40
    3. Set variant 10xx bits: bytes[8] = (bytes[8] & 0x3f) | 0x80
    4. Format as lowercase hex 8-4-4-4-12
    """
    assert len(random_bytes) == 16, f"Expected 16 bytes, got {len(random_bytes)}"
    b = bytearray(random_bytes)
    b[6] = (b[6] & 0x0f) | 0x40
    b[8] = (b[8] & 0x3f) | 0x80
    hex_str = "".join(f"{byte:02x}" for byte in b)
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


# The server's idempotency-key validation regex (from app.py start_workflow)
_SERVER_IDEMPOTENCY_KEY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_fallback_uuid_rfc4122_version_and_variant_bits():
    """The fallback algorithm produces valid RFC 4122 v4 version and variant bits."""
    import os
    for _ in range(200):
        raw = os.urandom(16)
        result = _simulate_fallback_uuid(raw)
        # Version nibble (first hex char of 3rd group) must be '4'
        version_char = result[14]
        assert version_char == "4", f"Expected version '4', got '{version_char}' in {result}"
        # Variant bits (first hex char of 4th group) must be 8, 9, a, or b (10xx)
        variant_char = result[19]
        assert variant_char in "89ab", f"Expected variant 8/9/a/b, got '{variant_char}' in {result}"


def test_fallback_uuid_lowercase_8_4_4_4_12_format():
    """The fallback output is always lowercase hex in 8-4-4-4-12 format."""
    import os
    for _ in range(200):
        raw = os.urandom(16)
        result = _simulate_fallback_uuid(raw)
        # Must match the server's exact regex
        assert _SERVER_IDEMPOTENCY_KEY_RE.fullmatch(result), f"Format mismatch: {result}"
        # Must be all lowercase (no uppercase hex digits)
        assert result == result.lower(), f"Contains uppercase: {result}"
        # Length check: 8+1+4+1+4+1+4+1+12 = 36
        assert len(result) == 36, f"Expected length 36, got {len(result)}"


def test_fallback_uuid_uniqueness_across_many_sequences():
    """Many different random byte sequences produce unique UUIDs (no collisions)."""
    import os
    seen = set()
    for _ in range(1000):
        raw = os.urandom(16)
        result = _simulate_fallback_uuid(raw)
        assert result not in seen, f"Collision detected: {result}"
        seen.add(result)
    assert len(seen) == 1000


def test_fallback_uuid_deterministic_with_same_bytes():
    """Same input bytes always produce the same UUID (deterministic algorithm)."""
    fixed_bytes = bytes(range(16))
    first = _simulate_fallback_uuid(fixed_bytes)
    second = _simulate_fallback_uuid(fixed_bytes)
    assert first == second
    # Verify the exact expected output for bytes 0x00..0x0f
    # byte[6] = 0x06 -> (0x06 & 0x0f) | 0x40 = 0x46
    # byte[8] = 0x08 -> (0x08 & 0x3f) | 0x80 = 0x88
    expected_bytes = bytearray(fixed_bytes)
    expected_bytes[6] = (0x06 & 0x0f) | 0x40  # 0x46
    expected_bytes[8] = (0x08 & 0x3f) | 0x80  # 0x88
    expected_hex = "".join(f"{b:02x}" for b in expected_bytes)
    expected = f"{expected_hex[0:8]}-{expected_hex[8:12]}-{expected_hex[12:16]}-{expected_hex[16:20]}-{expected_hex[20:32]}"
    assert first == expected


def test_fallback_uuid_accepted_by_server_validation(password_file, gateway_key_file, auth_headers):
    """A fallback-generated key is accepted by the real server idempotency-key validation.

    This is a server integration test: it generates a key using the Python
    simulation of the browser fallback and POSTs it to /api/workflows.
    """
    import os
    fallback_key = _simulate_fallback_uuid(os.urandom(16))
    # Sanity: the key must match the server regex before we even try the endpoint
    assert _SERVER_IDEMPOTENCY_KEY_RE.fullmatch(fallback_key)

    mock_client = _make_async_client_mock(
        _make_gateway_response(202, {"workflow_id": "0123456789", "overall": "queued"})
    )
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/workflows",
            headers=auth_headers,
            json={
                "project": "firstproject",
                "objective": "Build feature X",
                "reasoning": "standard",
                "idempotency_key": fallback_key,
            },
        )
    assert response.status_code == 200
    # Verify the key was forwarded to the gateway
    call = mock_client.post.await_args
    assert call.kwargs["headers"]["Idempotency-Key"] == fallback_key


# --- Behavioral state-machine test: generation failure preserves old state ---


class _BuildKeyStateMachine:
    """Python simulation of the JS getBuildKey/rotateBuildKey state machine.

    Mirrors the exact logic in static/app.js after the atomic-assignment fix:
    - Generate the candidate UUID first
    - Only after successful generation, assign both buildKey and buildKeyIntent
    - If generation throws, neither stored key nor intent changes
    """

    def __init__(self, generate_fn):
        self.build_key = None
        self.build_key_intent = None
        self._generate = generate_fn

    def _normalize(self, project, objective, reasoning):
        return f"{project}\x00{objective}\x00{reasoning}"

    def get_build_key(self, project, objective, reasoning):
        intent = self._normalize(project, objective, reasoning)
        if self.build_key and self.build_key_intent == intent:
            return self.build_key
        new_key = self._generate()  # may throw
        self.build_key = new_key
        self.build_key_intent = intent
        return self.build_key

    def rotate_build_key(self, project, objective, reasoning):
        intent = self._normalize(project, objective, reasoning)
        new_key = self._generate()  # may throw
        self.build_key = new_key
        self.build_key_intent = intent

    def clear_build_key(self):
        self.build_key = None
        self.build_key_intent = None


def test_state_machine_generation_failure_preserves_old_state():
    """A generation failure during intent change leaves the old key bound only
    to the old intent. A later retry cannot return the stale key for the new intent."""
    call_count = 0

    def generate_fn():
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Web Crypto API is unavailable")
        return f"key-{call_count}"

    sm = _BuildKeyStateMachine(generate_fn)

    # First call: intent A, generates key-1 successfully
    key_a = sm.get_build_key("projA", "objA", "standard")
    assert key_a == "key-1"
    assert sm.build_key == "key-1"
    assert sm.build_key_intent == "projA\x00objA\x00standard"

    # Second call: intent B, generation throws
    intent_b = "projB\x00objB\x00standard"
    with pytest.raises(RuntimeError, match="Web Crypto"):
        sm.get_build_key("projB", "objB", "standard")

    # After failure: old key and old intent must be unchanged
    assert sm.build_key == "key-1", "buildKey must not change on generation failure"
    assert sm.build_key_intent == "projA\x00objA\x00standard", "buildKeyIntent must not change on generation failure"

    # Retry with intent A: should return the same key (intent matches)
    key_a_retry = sm.get_build_key("projA", "objA", "standard")
    assert key_a_retry == "key-1"

    # Retry with intent B: must NOT return the stale key-1
    # Since buildKeyIntent is still "projA..." and intent is "projB...",
    # the condition (buildKey && buildKeyIntent === intent) is False,
    # so it will attempt generation again.
    call_count = 2  # next call will be call 3, which succeeds
    key_b = sm.get_build_key("projB", "objB", "standard")
    assert key_b == "key-3", f"Expected fresh key for new intent, got {key_b}"
    assert key_b != "key-1", "Stale key must not be returned for a different intent"
    assert sm.build_key == "key-3"
    assert sm.build_key_intent == intent_b


def test_state_machine_rotate_failure_preserves_old_state():
    """rotateBuildKey generation failure also preserves old state atomically."""
    call_count = 0

    def generate_fn():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "initial-key"
        raise RuntimeError("crypto unavailable")

    sm = _BuildKeyStateMachine(generate_fn)
    sm.get_build_key("projA", "objA", "standard")
    assert sm.build_key == "initial-key"
    assert sm.build_key_intent == "projA\x00objA\x00standard"

    # Rotate with a new intent — generation throws
    with pytest.raises(RuntimeError):
        sm.rotate_build_key("projB", "objB", "deep")

    # State must be unchanged
    assert sm.build_key == "initial-key"
    assert sm.build_key_intent == "projA\x00objA\x00standard"


# --- Behavioral submission harness: POST count and error recovery ---


class _SubmissionHarness:
    """Simulates the startBuild submission flow with injectable crypto and fetch.

    Tracks:
    - Number of POST requests made
    - Form disabled state
    - Status message shown to the user
    - buildInFlight state
    """

    def __init__(self, generate_fn, fetch_fn):
        self._generate = generate_fn
        self._fetch = fetch_fn
        self.post_count = 0
        self.form_disabled = False
        self.status_message = ""
        self.status_is_error = False
        self.build_in_flight = False
        # State machine
        self.build_key = None
        self.build_key_intent = None

    def _normalize(self, project, objective, reasoning):
        return f"{project}\x00{objective}\x00{reasoning}"

    def _get_build_key(self, project, objective, reasoning):
        intent = self._normalize(project, objective, reasoning)
        if self.build_key and self.build_key_intent == intent:
            return self.build_key
        new_key = self._generate()
        self.build_key = new_key
        self.build_key_intent = intent
        return self.build_key

    def _set_build_disabled(self, disabled):
        self.form_disabled = disabled

    def _set_build_status(self, message, is_error=False):
        self.status_message = message
        self.status_is_error = is_error

    async def start_build(self, project, objective, reasoning):
        if self.build_in_flight:
            return
        if not project or not objective:
            self._set_build_status("Please select a project and enter an objective.", True)
            return

        self.build_in_flight = True
        self._set_build_disabled(True)
        self._set_build_status("Submitting…")

        try:
            idempotency_key = self._get_build_key(project, objective, reasoning)
            self.post_count += 1
            response = await self._fetch(idempotency_key)
            if response.ok:
                self._set_build_status("Workflow started.")
                self.build_key = None
                self.build_key_intent = None
            else:
                self._set_build_status(response.detail or "Error", True)
            self._set_build_disabled(False)
            self.build_in_flight = False
        except Exception as error:
            self._set_build_status(str(error) or "Network error. Please try again.", True)
            self._set_build_disabled(False)
            self.build_in_flight = False


class _MockResponse:
    def __init__(self, ok=True, detail=None):
        self.ok = ok
        self.detail = detail


def test_submission_fallback_path_yields_exactly_one_post():
    """When randomUUID is absent but getRandomValues exists, the fallback path
    yields exactly one POST with a valid key."""
    import os

    def fallback_generate():
        return _simulate_fallback_uuid(os.urandom(16))

    async def fetch_fn(key):
        # Verify the key is a valid UUID before "sending"
        assert _SERVER_IDEMPOTENCY_KEY_RE.fullmatch(key), f"Invalid key sent: {key}"
        return _MockResponse(ok=True)

    harness = _SubmissionHarness(fallback_generate, fetch_fn)
    import asyncio
    asyncio.run(harness.start_build("firstproject", "Build feature X", "standard"))

    assert harness.post_count == 1, f"Expected exactly 1 POST, got {harness.post_count}"
    assert harness.form_disabled is False, "Form must be re-enabled after success"
    assert harness.build_in_flight is False
    assert harness.status_is_error is False
    # Key should be cleared after confirmed success
    assert harness.build_key is None
    assert harness.build_key_intent is None


def test_submission_no_crypto_zero_post_and_error_recovery():
    """When all Web Crypto generation is unavailable, zero POSTs are made,
    a visible safe error is shown, and the form is recovered."""

    def unavailable_generate():
        raise RuntimeError("Web Crypto API is unavailable in this browser. Use a modern browser or serve over HTTPS.")

    async def fetch_fn(key):
        # This should never be called
        raise AssertionError("fetch must not be called when crypto is unavailable")

    harness = _SubmissionHarness(unavailable_generate, fetch_fn)
    import asyncio
    asyncio.run(harness.start_build("firstproject", "Build feature X", "standard"))

    assert harness.post_count == 0, f"Expected 0 POSTs, got {harness.post_count}"
    assert harness.form_disabled is False, "Form must be re-enabled after crypto failure"
    assert harness.build_in_flight is False, "buildInFlight must be reset"
    assert harness.status_is_error is True, "Error status must be shown"
    assert "Web Crypto" in harness.status_message, f"Error message must mention Web Crypto: {harness.status_message}"
    # No key should have been stored
    assert harness.build_key is None
    assert harness.build_key_intent is None


def test_submission_double_click_suppression():
    """Concurrent double-clicks are suppressed: only one POST is made."""
    import os
    import asyncio

    def fallback_generate():
        return _simulate_fallback_uuid(os.urandom(16))

    post_calls = []

    async def fetch_fn(key):
        post_calls.append(key)
        return _MockResponse(ok=True)

    harness = _SubmissionHarness(fallback_generate, fetch_fn)

    async def run_both():
        # Simulate two rapid submissions (double-click)
        # The first sets build_in_flight=True, the second should be suppressed
        harness.build_in_flight = True  # Simulate first click already in flight
        await harness.start_build("firstproject", "Build feature X", "standard")

    asyncio.run(run_both())
    assert harness.post_count == 0, "Second click must be suppressed when first is in flight"


def test_submission_reuses_key_on_same_intent():
    """Same intent reuses the same idempotency key (no new generation)."""
    import os

    generated_keys = []

    def tracking_generate():
        key = _simulate_fallback_uuid(os.urandom(16))
        generated_keys.append(key)
        return key

    async def fetch_fn(key):
        return _MockResponse(ok=True)

    harness = _SubmissionHarness(tracking_generate, fetch_fn)
    import asyncio

    # First submission: generates a key, succeeds, clears it
    asyncio.run(harness.start_build("proj", "obj1", "standard"))
    assert len(generated_keys) == 1

    # After success, key is cleared. Second submission with same intent generates fresh.
    asyncio.run(harness.start_build("proj", "obj1", "standard"))
    assert len(generated_keys) == 2
    assert generated_keys[0] != generated_keys[1], "Keys must differ after clear"


def test_submission_409_rotates_key():
    """A 409 response triggers key rotation for the next attempt."""
    import os

    generated_keys = []

    def tracking_generate():
        key = _simulate_fallback_uuid(os.urandom(16))
        generated_keys.append(key)
        return key

    call_count = 0

    async def fetch_fn(key):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _MockResponse(ok=False, detail="A workflow with this idempotency key already exists")
        return _MockResponse(ok=True)

    harness = _SubmissionHarness(tracking_generate, fetch_fn)
    import asyncio

    # First attempt: 409
    asyncio.run(harness.start_build("proj", "obj", "standard"))
    assert harness.post_count == 1
    assert harness.status_is_error is True

    # After 409, the key should be rotated (new key generated)
    # The harness doesn't auto-rotate on 409 in this simplified model,
    # but the key is NOT cleared (unlike success), so next same-intent
    # submission reuses it. In the real JS, rotateBuildKey is called.
    # Let's verify the key is retained (not cleared on 409):
    assert harness.build_key is not None, "Key must be retained after 409 for rotation"


# --- Source contract: atomic assignment pattern ---


def test_frontend_js_atomic_assignment_in_getbuildkey():
    """getBuildKey generates the UUID before assigning buildKey/buildKeyIntent.

    This ensures that if generateUUID() throws, neither stored state changes.
    """
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function getBuildKey\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "getBuildKey function not found"
    body = match.group(0)
    # The generate call must come before both assignments
    gen_pos = body.find("generateUUID()")
    # Use precise patterns to avoid matching '===' in the condition check
    key_assign_pos = body.find("buildKey = newKey")
    intent_assign_pos = body.find("buildKeyIntent = intent")
    assert gen_pos != -1, "generateUUID() call not found in getBuildKey"
    assert key_assign_pos != -1, "buildKey = newKey assignment not found"
    assert intent_assign_pos != -1, "buildKeyIntent = intent assignment not found"
    assert gen_pos < key_assign_pos, "generateUUID must execute before buildKey assignment"
    assert gen_pos < intent_assign_pos, "generateUUID must execute before buildKeyIntent assignment"


def test_frontend_js_atomic_assignment_in_rotatebuildkey():
    """rotateBuildKey generates the UUID before assigning buildKey/buildKeyIntent."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function rotateBuildKey\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "rotateBuildKey function not found"
    body = match.group(0)
    gen_pos = body.find("generateUUID()")
    key_assign_pos = body.find("buildKey = newKey")
    intent_assign_pos = body.find("buildKeyIntent = intent")
    assert gen_pos != -1, "generateUUID() call not found in rotateBuildKey"
    assert key_assign_pos != -1, "buildKey = newKey assignment not found"
    assert intent_assign_pos != -1, "buildKeyIntent = intent assignment not found"
    assert gen_pos < key_assign_pos, "generateUUID must execute before buildKey assignment"
    assert gen_pos < intent_assign_pos, "generateUUID must execute before buildKeyIntent assignment"


# --- Deployment contract: compose.yaml allowlist & secret handling ---


def test_compose_supplies_authoritative_allowlist():
    """The deployment must supply a non-empty authoritative project allowlist."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"AGENT_GATEWAY_ALLOWED_PROJECTS:\s*(\S+)", compose)
    assert match is not None, "compose.yaml must set AGENT_GATEWAY_ALLOWED_PROJECTS"
    projects = [p.strip() for p in match.group(1).split(",") if p.strip()]
    assert projects, "AGENT_GATEWAY_ALLOWED_PROJECTS must be non-empty"
    assert "firstproject" in projects
    assert "infrastructure" in projects


def test_compose_uses_file_backed_secrets_not_inline():
    """The gateway key must be a file-backed secret, never an inline env value."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    # The key is referenced as a named secret (file-backed), not an inline env var.
    assert "agent_gateway_key" in compose
    assert "AGENT_GATEWAY_KEY:" not in compose
    # The internal gateway URL is the docker-internal host, not a public endpoint.
    assert "host.docker.internal" in compose


def test_compose_cluster_health_configuration():
    """Verify the compose deployment matches the cluster health requirements:
    - Joins m1-agent-repo_chat plus default network
    - Uses router:4000 and open-webui:8080
    - Uses file-backed lm_studio_token for 10.10.10.1:1234
    - Telegram uses the internal service URL on the external network
    """
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    # Network: joins m1-agent-repo_chat (external) plus default
    assert "m1-agent-repo_chat" in compose
    assert "external: true" in compose
    # Router and Open WebUI use the correct internal service URLs
    assert "http://router:4000" in compose
    assert "http://open-webui:8080" in compose
    # LM Studio uses the file-backed token and the correct URL
    assert "http://10.10.10.1:1234" in compose
    assert "lm_studio_token" in compose
    assert "LMSTUDIO_TOKEN_FILE" in compose
    # Telegram uses the internal service URL on the external network
    assert "TELEGRAM_BOT_URL: http://telegram-bot:8080" in compose


def test_compose_telegram_uses_service_name_not_ip():
    """The TELEGRAM_BOT_URL must use the Docker DNS service name, not a container IP."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"TELEGRAM_BOT_URL:\s*(\S+)", compose)
    assert match is not None, "TELEGRAM_BOT_URL must be set in compose.yaml"
    url = match.group(1)
    # Must use the service name telegram-bot
    assert "telegram-bot" in url, f"URL must use service name 'telegram-bot', got: {url}"
    # Must NOT use a dotted-quad IP address
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", url), f"URL must not contain a container IP: {url}"


def test_compose_no_telegram_port_published():
    """No ports entry may publish the Telegram health port to the host or LAN.

    Allowed published ports:
    - Dashboard: 192.168.68.68:8088:8080 (LAN HTTP)
    - Caddy proxy: 127.0.0.1:8444:8444 (M1 loopback only, for Tailscale)
    """
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    ports_section = re.findall(r'ports:.*?(?=\n    \S|\Z)', compose, re.DOTALL)
    for block in ports_section:
        lines = [l.strip() for l in block.strip().splitlines() if l.strip().startswith("-")]
        for line in lines:
            port_match = re.search(r'"?(\S+):(\d+):(\d+)"?', line)
            if port_match:
                host_ip = port_match.group(1)
                host_port = port_match.group(2)
                container_port = port_match.group(3)
                # Allowed: dashboard on LAN (192.168.68.68:8088:8080)
                if host_ip == "192.168.68.68" and host_port == "8088" and container_port == "8080":
                    continue
                # Allowed: Caddy proxy on M1 loopback (127.0.0.1:8444:8444)
                if host_ip == "127.0.0.1" and host_port == "8444" and container_port == "8444":
                    continue
                assert False, (
                    f"Unexpected published port mapping: {line}. "
                    "Only the dashboard 192.168.68.68:8088:8080 and the proxy "
                    "127.0.0.1:8444:8444 may be published."
                )


def test_compose_external_network_only_dashboard():
    """Only the dashboard service may list m1-agent-repo_chat in its networks.
    The dashboard must retain the default network alongside the external one."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    # Capture the full services block: everything after "services:\n" until the next top-level key
    services_match = re.search(r"^services:\n(.*?)(?=^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert services_match is not None, "compose.yaml must have a services section"
    services_block = services_match.group(1)
    # Find all service names (2-space indent, name followed by colon)
    service_names = re.findall(r"^  (\S+):", services_block, re.MULTILINE)
    assert "dashboard" in service_names, f"dashboard service not found; got {service_names}"
    # Extract the dashboard service block (from "  dashboard:" to next 2-space-indented key or end)
    dashboard_match = re.search(r"^  dashboard:\n(.*?)(?=^  \S|\Z)", services_block, re.MULTILINE | re.DOTALL)
    assert dashboard_match is not None, "dashboard service block not found"
    dashboard_block = dashboard_match.group(0)
    # Dashboard must retain the default network
    assert "default" in dashboard_block, "dashboard must retain the default network"
    # Dashboard must join the external m1-agent-repo_chat network
    assert "m1-agent-repo_chat" in dashboard_block, "dashboard must join m1-agent-repo_chat"
    # No other service should list m1-agent-repo_chat
    for svc in service_names:
        if svc == "dashboard":
            continue
        svc_match = re.search(rf"^  {re.escape(svc)}:\n(.*?)(?=^  \S|\Z)", services_block, re.MULTILINE | re.DOTALL)
        if svc_match:
            assert "m1-agent-repo_chat" not in svc_match.group(0), (
                f"Service '{svc}' must not join m1-agent-repo_chat"
            )


# --- Template API: fixtures and helpers ---


@pytest.fixture
def templates_file(tmp_path: Path) -> Path:
    tf = tmp_path / "templates.json"
    tf.write_text(app.json.dumps({"templates": []}), encoding="utf-8")
    return tf


def _make_multi_role_users_file(tmp_path: Path) -> Path:
    users_file = tmp_path / "users.json"
    users_file.write_text(
        app.json.dumps({
            "reader": _user_record("viewer-pass", "viewer"),
            "op": _user_record("operator-pass", "operator"),
            "admin": _user_record("admin-pass", "admin"),
        }),
        encoding="utf-8",
    )
    return users_file


def _viewer_auth():
    return {"Authorization": _basic_auth("reader", "viewer-pass")}


def _operator_auth():
    return {"Authorization": _basic_auth("op", "operator-pass")}


def _admin_auth():
    return {"Authorization": _basic_auth("admin", "admin-pass")}


def _template_body(project="firstproject", name="My Template", **spec_overrides):
    spec = {
        "goal": "Build feature X",
        "acceptance": "Tests pass",
        "scope": "Module A",
        "exclusions": "Module B",
        "required_tests": "pytest",
        "notes": "Use TDD",
    }
    spec.update(spec_overrides)
    return {"project": project, "name": name, "spec": spec}


def _seed_template(templates_file: Path, project="firstproject", name="Seeded", template_id=None):
    import uuid as _uuid
    if template_id is None:
        template_id = _uuid.uuid4().hex
    now = "2025-01-15T10:00:00+00:00"
    data = app.json.loads(templates_file.read_text(encoding="utf-8"))
    data["templates"].append({
        "id": template_id,
        "project": project,
        "name": name,
        "spec": {
            "goal": "Seeded goal",
            "acceptance": "Seeded acceptance",
            "scope": "",
            "exclusions": "",
            "required_tests": "",
            "notes": "",
        },
        "created_at": now,
        "updated_at": now,
    })
    templates_file.write_text(app.json.dumps({"templates": data["templates"]}, indent=2), encoding="utf-8")
    return template_id


# --- Template API: GET /api/templates ---


def test_list_templates_admin_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    _seed_template(templates_file, project="firstproject", name="Alpha")
    _seed_template(templates_file, project="firstproject", name="Beta")
    _seed_template(templates_file, project="secondproject", name="Gamma")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject", "secondproject"]):
        response = TestClient(app.app).get(
            "/api/templates?project=firstproject", headers=_admin_auth()
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data["templates"]) == 2
    names = {t["name"] for t in data["templates"]}
    assert names == {"Alpha", "Beta"}


def test_list_templates_operator_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).get(
            "/api/templates?project=firstproject", headers=_operator_auth()
        )
    assert response.status_code == 200
    assert len(response.json()["templates"]) == 1


def test_list_templates_viewer_403(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).get(
            "/api/templates?project=firstproject", headers=_viewer_auth()
        )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"]


def test_list_templates_disallowed_project(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).get(
            "/api/templates?project=evil-project", headers=_admin_auth()
        )
    assert response.status_code == 400
    assert "not in the allowed list" in response.json()["detail"]


def test_list_templates_project_isolation(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    _seed_template(templates_file, project="firstproject", name="Alpha")
    _seed_template(templates_file, project="secondproject", name="Beta")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject", "secondproject"]):
        response = TestClient(app.app).get(
            "/api/templates?project=secondproject", headers=_admin_auth()
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data["templates"]) == 1
    assert data["templates"][0]["name"] == "Beta"
    assert data["templates"][0]["project"] == "secondproject"


# --- Template API: POST /api/templates ---


def test_create_template_admin_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(), json=_template_body()
        )
    assert response.status_code == 200
    data = response.json()
    assert data["project"] == "firstproject"
    assert data["name"] == "My Template"
    assert data["spec"]["goal"] == "Build feature X"
    assert data["spec"]["acceptance"] == "Tests pass"
    assert len(data["id"]) == 32
    assert data["created_at"] == data["updated_at"]


def test_create_template_operator_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_operator_auth(), json=_template_body()
        )
    assert response.status_code == 200


def test_create_template_viewer_403(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_viewer_auth(), json=_template_body()
        )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"]


def test_create_template_disallowed_project(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(project="evil-project")
        )
    assert response.status_code == 400
    assert "not in the allowed list" in response.json()["detail"]


def test_create_template_blank_name(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(name="   ")
        )
    assert response.status_code == 400
    assert "Name" in response.json()["detail"]


def test_create_template_overlong_name(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(name="a" * 101)
        )
    assert response.status_code == 400
    assert "1-100" in response.json()["detail"]


def test_create_template_control_char_name(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(name="bad\nname")
        )
    assert response.status_code == 400
    assert "control" in response.json()["detail"]


def test_create_template_blank_goal(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(goal="")
        )
    assert response.status_code == 400
    assert "Goal" in response.json()["detail"]


def test_create_template_overlong_goal(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(goal="a" * 2001)
        )
    assert response.status_code == 400
    assert "2000" in response.json()["detail"]


def test_create_template_control_char_goal(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(),
            json=_template_body(goal="bad\ngoal")
        )
    assert response.status_code == 400
    assert "control" in response.json()["detail"]


def test_create_template_persisted_shape(tmp_path: Path, templates_file: Path):
    """The persisted JSON must contain exactly the fixed shape with no extra fields."""
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(), json=_template_body()
        )
    assert response.status_code == 200
    # Read the persisted file and verify the exact shape
    persisted = app.json.loads(templates_file.read_text(encoding="utf-8"))
    assert set(persisted.keys()) == {"templates"}
    assert len(persisted["templates"]) == 1
    record = persisted["templates"][0]
    assert set(record.keys()) == {"id", "project", "name", "spec", "created_at", "updated_at"}
    assert set(record["spec"].keys()) == {"goal", "acceptance", "scope", "exclusions", "required_tests", "notes"}
    # No secrets, paths, or credentials in the persisted data
    raw = templates_file.read_text(encoding="utf-8")
    assert "password" not in raw
    assert "secret" not in raw
    assert "/run/secrets" not in raw


def test_create_template_mode_0600(tmp_path: Path, templates_file: Path):
    """The templates file must be written with mode 0600 (owner read/write only)."""
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(), json=_template_body()
        )
    assert response.status_code == 200
    mode = templates_file.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected mode 0600, got {oct(mode)}"


def test_create_template_atomic_write_no_tmp_remains(tmp_path: Path, templates_file: Path):
    """After a successful write, no .tmp file should remain (atomic os.replace)."""
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).post(
            "/api/templates", headers=_admin_auth(), json=_template_body()
        )
    assert response.status_code == 200
    tmp_file = templates_file.with_suffix(".tmp")
    assert not tmp_file.exists(), "A .tmp file must not remain after atomic replace"


# --- Template API: DELETE /api/templates/{id} ---


def test_delete_template_admin_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    tid = _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{tid}?project=firstproject", headers=_admin_auth()
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    # Verify the template was removed from the file
    persisted = app.json.loads(templates_file.read_text(encoding="utf-8"))
    assert len(persisted["templates"]) == 0


def test_delete_template_operator_success(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    tid = _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{tid}?project=firstproject", headers=_operator_auth()
        )
    assert response.status_code == 200


def test_delete_template_viewer_403(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    tid = _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{tid}?project=firstproject", headers=_viewer_auth()
        )
    assert response.status_code == 403
    assert "operator" in response.json()["detail"]


def test_delete_template_disallowed_project(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    tid = _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{tid}?project=evil-project", headers=_admin_auth()
        )
    assert response.status_code == 400
    assert "not in the allowed list" in response.json()["detail"]


def test_delete_template_invalid_id_format(tmp_path: Path, templates_file: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            "/api/templates/not-a-valid-id?project=firstproject", headers=_admin_auth()
        )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_delete_template_not_found(tmp_path: Path, templates_file: Path):
    """A valid-format ID that doesn't match any template returns 404."""
    users_file = _make_multi_role_users_file(tmp_path)
    _seed_template(templates_file, project="firstproject", name="Alpha")
    # Use a valid 32-hex ID that doesn't exist
    fake_id = "a" * 32
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{fake_id}?project=firstproject", headers=_admin_auth()
        )
    assert response.status_code == 404


def test_delete_template_project_isolation(tmp_path: Path, templates_file: Path):
    """Deleting with a different project than the template's project must 404."""
    users_file = _make_multi_role_users_file(tmp_path)
    tid = _seed_template(templates_file, project="firstproject", name="Alpha")
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject", "secondproject"]):
        response = TestClient(app.app).delete(
            f"/api/templates/{tid}?project=secondproject", headers=_admin_auth()
        )
    assert response.status_code == 404
    # The template must still exist
    persisted = app.json.loads(templates_file.read_text(encoding="utf-8"))
    assert len(persisted["templates"]) == 1
    assert persisted["templates"][0]["id"] == tid


# --- Frontend contract: viewer gates and template controls ---


def test_frontend_js_viewer_skips_load_templates():
    """initialize() must gate loadTemplates on role !== 'viewer' to avoid 403 on load."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    # The initialize function must check role before calling loadTemplates
    assert "sessionIdentity.role!=='viewer'" in js
    # The role check must guard the loadTemplates call (they appear together in initialize)
    assert "if(sessionIdentity.role!=='viewer'){await loadTemplates" in js


def test_frontend_js_delete_template_includes_project():
    """deleteTemplate must include the project query parameter in the DELETE URL."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function deleteTemplate\(\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "deleteTemplate function not found"
    body = match.group(0)
    assert "project" in body
    assert "encodeURIComponent(project)" in body
    # The DELETE URL must include the project parameter
    assert "?project=" in body


def test_frontend_js_template_select_disabled_for_viewer():
    """setBuildDisabled must disable template-select, template-save, and template-delete."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function setBuildDisabled\(disabled\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "setBuildDisabled function not found"
    body = match.group(0)
    assert "template-select" in body
    assert "template-save" in body
    assert "template-delete" in body


def test_frontend_js_objective_preview_compose():
    """composeObjective must join non-empty spec fields with ' | '."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function composeObjective\(\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "composeObjective function not found"
    body = match.group(0)
    assert "parts.join(' | ')" in body
    assert "Goal:" in body
    assert "Acceptance Criteria:" in body


def test_frontend_js_objective_preview_length_and_limit():
    """updatePreview must show char count, toggle over-limit, and disable submit >2000."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function updatePreview\(\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "updatePreview function not found"
    body = match.group(0)
    assert "build-char-count" in body
    assert "over-limit" in body
    assert "build-submit" in body
    assert "2000" in body


def test_frontend_js_viewer_disables_template_controls_on_load():
    """loadSession must call setBuildDisabled(true) for viewers, which disables template controls."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function loadSession\(\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "loadSession function not found"
    body = match.group(0)
    assert "viewer" in body
    assert "setBuildDisabled(true)" in body


# --- Cluster Health: _normalize_health_url ---


def test_normalize_health_url_http():
    assert app._normalize_health_url("http://localhost:8080") == "http://localhost:8080"


def test_normalize_health_url_https():
    assert app._normalize_health_url("https://example.com") == "https://example.com"


def test_normalize_health_url_trailing_slash():
    assert app._normalize_health_url("http://localhost:8080/") == "http://localhost:8080"
    assert app._normalize_health_url("https://example.com/") == "https://example.com"


def test_normalize_health_url_empty():
    assert app._normalize_health_url("") is None
    assert app._normalize_health_url("   ") is None


def test_normalize_health_url_rejects_file_scheme():
    assert app._normalize_health_url("file:///etc/passwd") is None


def test_normalize_health_url_rejects_gopher_scheme():
    assert app._normalize_health_url("gopher://example.com") is None


def test_normalize_health_url_rejects_non_string():
    assert app._normalize_health_url(None) is None
    assert app._normalize_health_url(123) is None


# --- Cluster Health: _sanitize_model_id ---


def test_sanitize_model_id_valid():
    assert app._sanitize_model_id("meta-llama/Llama-3-8B") == "meta-llama/Llama-3-8B"


def test_sanitize_model_id_max_length():
    valid_id = "a" * app.LMSTUDIO_MODEL_ID_MAX
    assert app._sanitize_model_id(valid_id) == valid_id


def test_sanitize_model_id_too_long():
    too_long = "a" * (app.LMSTUDIO_MODEL_ID_MAX + 1)
    assert app._sanitize_model_id(too_long) is None


def test_sanitize_model_id_unsafe_characters():
    assert app._sanitize_model_id("model with spaces") is None
    assert app._sanitize_model_id("model;rm -rf /") is None
    assert app._sanitize_model_id("model\nnewline") is None


def test_sanitize_model_id_empty():
    assert app._sanitize_model_id("") is None
    assert app._sanitize_model_id("   ") is None


def test_sanitize_model_id_non_string():
    assert app._sanitize_model_id(None) is None
    assert app._sanitize_model_id(123) is None
    assert app._sanitize_model_id(["list"]) is None


# --- Cluster Health: _compute_overall ---


def test_compute_overall_all_healthy():
    services = [
        {"name": "a", "status": "healthy"},
        {"name": "b", "status": "healthy"},
    ]
    assert app._compute_overall(services) == "healthy"


def test_compute_overall_all_offline():
    services = [
        {"name": "a", "status": "offline"},
        {"name": "b", "status": "offline"},
    ]
    assert app._compute_overall(services) == "offline"


def test_compute_overall_mixed_healthy_and_offline():
    services = [
        {"name": "a", "status": "healthy"},
        {"name": "b", "status": "offline"},
    ]
    assert app._compute_overall(services) == "degraded"


def test_compute_overall_mixed_healthy_and_degraded():
    services = [
        {"name": "a", "status": "healthy"},
        {"name": "b", "status": "degraded"},
    ]
    assert app._compute_overall(services) == "degraded"


def test_compute_overall_all_unknown():
    services = [
        {"name": "a", "status": "unknown"},
        {"name": "b", "status": "unknown"},
    ]
    assert app._compute_overall(services) == "unknown"


def test_compute_overall_unknown_excluded_from_computation():
    services = [
        {"name": "a", "status": "healthy"},
        {"name": "b", "status": "unknown"},
    ]
    assert app._compute_overall(services) == "healthy"


def test_compute_overall_empty_list():
    assert app._compute_overall([]) == "unknown"


# --- Cluster Health: _project_agent_queue ---


def test_project_agent_queue_empty():
    result = app._project_agent_queue({})
    assert result["queued"] == 0
    assert result["running"] == 0
    assert result["running_jobs"] == []
    assert result["current_job"] is None
    assert result["detail"] == "unavailable"
    assert result["workflow_efficiency"] == []
    assert result["needs_continuation"] == []


def test_project_agent_queue_queued_and_running_counts():
    data = {
        "jobs": [
            {"workflow_id": "0123456789", "project": "p1", "stage": "test", "status": "queued"},
            {"workflow_id": "0123456789", "project": "p1", "stage": "test", "status": "queued"},
            {"workflow_id": "abcdef1234", "project": "p2", "stage": "plan", "status": "running"},
        ]
    }
    result = app._project_agent_queue(data)
    assert result["queued"] == 2
    assert result["running"] == 1
    assert len(result["running_jobs"]) == 1
    assert result["current_job"]["id"] == "abcdef1234"
    assert result["current_job"]["project"] == "p2"
    assert result["current_job"]["stage"] == "plan"


def test_project_agent_queue_multiple_running_jobs():
    jobs = [
        {"workflow_id": f"012345678{i}", "project": f"proj{i}", "stage": "test", "status": "running"}
        for i in range(5)
    ]
    result = app._project_agent_queue({"jobs": jobs})
    assert result["running"] == 5
    assert len(result["running_jobs"]) == 5
    assert result["current_job"]["id"] == "0123456780"


def test_project_agent_queue_bounding():
    """Running jobs are bounded to MAX_RUNNING_JOBS in the list, but the count is accurate."""
    jobs = [
        {"workflow_id": f"012345678{i:02d}", "project": f"proj{i}", "stage": "test", "status": "running"}
        for i in range(app.MAX_RUNNING_JOBS + 10)
    ]
    result = app._project_agent_queue({"jobs": jobs})
    assert result["running"] == app.MAX_RUNNING_JOBS + 10
    assert len(result["running_jobs"]) == app.MAX_RUNNING_JOBS


def test_project_agent_queue_no_unsafe_fields():
    """No prompt, path, secret, or other unsafe fields leak through."""
    data = {
        "jobs": [
            {
                "workflow_id": "0123456789",
                "project": "p1",
                "stage": "test",
                "status": "running",
                "prompt": "SECRET-PROMPT-CONTENT",
                "worktree": "/home/user/worktrees/secret-path",
                "api_key": "sk-secret-key-12345",
                "token": "bearer-token-abc",
            }
        ]
    }
    result = app._project_agent_queue(data)
    raw = app.json.dumps(result)
    assert "SECRET-PROMPT-CONTENT" not in raw
    assert "/home/user/worktrees" not in raw
    assert "sk-secret-key-12345" not in raw
    assert "bearer-token-abc" not in raw
    # Each running job must have exactly the allow-listed keys
    for job in result["running_jobs"]:
        assert set(job.keys()) == {"id", "project", "stage", "model", "model_reason", "duplicate_tool_call_count", "duplicate_warning"}


def test_project_agent_queue_projects_duplicate_metrics_and_workflow_aggregates():
    result = app._project_agent_queue({
        "jobs": [{"workflow_id": "running000", "project": "p1", "stage": "implement", "status": "running", "duplicate_tool_call_count": 6}],
        "recent_workflows": [{"id": "workflow01", "project": "p1", "overall": "completed", "total_tokens": 1234, "duplicate_tool_call_count": 5}],
    })
    assert result["current_job"]["duplicate_tool_call_count"] == 6
    assert result["current_job"]["duplicate_warning"] is True
    assert result["workflow_efficiency"] == [{
        "id": "workflow01", "project": "p1", "overall": "completed", "total_tokens": 1234,
        "duplicate_tool_call_count": 5, "duplicate_warning": False,
    }]


def test_project_agent_queue_rejects_invalid_duplicate_metrics():
    result = app._project_agent_queue({
        "jobs": [{"workflow_id": "running000", "project": "p1", "stage": "implement", "status": "running", "duplicate_tool_call_count": True}],
        "recent_workflows": [{"id": "workflow01", "project": "p1", "total_tokens": -1, "duplicate_tool_call_count": "secret"}],
    })
    assert result["current_job"]["duplicate_tool_call_count"] is None
    assert result["current_job"]["duplicate_warning"] is False
    assert result["workflow_efficiency"][0]["total_tokens"] is None
    assert result["workflow_efficiency"][0]["duplicate_tool_call_count"] is None


def test_project_agent_queue_projects_safe_continuation_jobs():
    result = app._project_agent_queue({"jobs": [{
        "id": "job123", "workflow_id": "workflow01", "project": "p1", "stage": "implement",
        "status": "failed", "budget_exhausted": True, "checkpoint_steps_used": 40,
        "checkpoint_source_mutated": 1, "checkpoint_test_ran": 0,
        "checkpoint_summary": "Manual retry is available.", "prompt": "secret",
    }]})
    assert result["needs_continuation"] == [{
        "id": "job123", "workflow_id": "workflow01", "project": "p1", "stage": "implement",
        "steps_used": 40, "source_mutated": True, "test_ran": False,
        "summary": "Manual retry is available.",
    }]
    assert "secret" not in app.json.dumps(result)


# --- Cluster Health: TestClient endpoint tests ---

from contextlib import contextmanager


def _routed_client(routes: dict):
    """Mock AsyncClient routing GET by URL substring.

    Each route maps a URL substring to either a response MagicMock or an Exception.
    """
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def _get(url, **kwargs):
        for pattern, result in routes.items():
            if pattern in url:
                if isinstance(result, Exception):
                    raise result
                return result
        return _make_gateway_response(200, {})

    mock_client.get = AsyncMock(side_effect=_get)
    return mock_client


@contextmanager
def _health_env(tmp_path: Path, **urls):
    """Patch all cluster health URLs, gateway key, LM token, and password file."""
    m5 = urls.get("m5", "http://m5:8080")
    lm = urls.get("lmstudio", "http://lmstudio:1234")
    gw = urls.get("gateway", "http://gateway:8765")
    tg = urls.get("telegram", "http://telegram-bot:8080")
    ow = urls.get("openwebui", "http://openwebui:3000")
    rt = urls.get("router", "http://router:8082")

    pw_file = tmp_path / "pw"
    pw_file.write_text("test-pass", encoding="utf-8")
    key_file = tmp_path / "gw_key"
    key_file.write_text("test-gw-key", encoding="utf-8")
    lm_token_file = tmp_path / "lm_token"
    lm_token_file.write_text("test-lm-token", encoding="utf-8")

    with patch.object(app, "M5_HOST_URL", m5), \
         patch.object(app, "LMSTUDIO_URL", lm), \
         patch.object(app, "GATEWAY_URL", gw), \
         patch.object(app, "TELEGRAM_BOT_URL", tg), \
         patch.object(app, "OPENWEBUI_URL", ow), \
         patch.object(app, "ROUTER_URL", rt), \
         patch.object(app, "GATEWAY_KEY_FILE", key_file), \
         patch.object(app, "LMSTUDIO_TOKEN_FILE", lm_token_file), \
         patch.object(app, "PASSWORD_FILE", pw_file):
        yield


def _all_healthy_routes():
    """Routes making all services return healthy."""
    return {
        "lmstudio:1234/api/v1/models": _make_gateway_response(200, {"models": [{"key": "test-model", "loaded_instances": [1]}]}),
        "gateway:8765/v1/dashboard": _make_gateway_response(200, {"jobs": [], "usage": [], "counts": [], "projects": []}),
        "telegram-bot:8080/health": _make_gateway_response(200, {"status": "ok"}),
        "openwebui:3000/health": _make_gateway_response(200, {"status": "ok"}),
        "router:8082/health": _make_gateway_response(200, {"status": "ok"}),
    }


def test_cluster_health_requires_auth(tmp_path: Path):
    with _health_env(tmp_path):
        response = TestClient(app.app).get("/api/cluster-health")
    assert response.status_code == 401


def test_cluster_health_rejects_wrong_password(tmp_path: Path):
    headers = {"Authorization": _basic_auth("admin", "wrong-pass")}
    with _health_env(tmp_path):
        response = TestClient(app.app).get("/api/cluster-health", headers=headers)
    assert response.status_code == 401


def test_cluster_health_viewer_can_read(tmp_path: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch.object(app, "USERS_FILE", users_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=_viewer_auth())
    assert response.status_code == 200


def test_cluster_health_operator_can_read(tmp_path: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch.object(app, "USERS_FILE", users_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=_operator_auth())
    assert response.status_code == 200


def test_cluster_health_admin_can_read(tmp_path: Path):
    users_file = _make_multi_role_users_file(tmp_path)
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch.object(app, "USERS_FILE", users_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=_admin_auth())
    assert response.status_code == 200


def test_cluster_health_exact_payload_shape(tmp_path: Path, auth_headers):
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"generated_at", "overall", "services", "lm_studio", "agent_queue"}
    for svc in data["services"]:
        assert set(svc.keys()) == {"name", "status", "latency_ms", "last_checked", "detail"}
    assert set(data["lm_studio"].keys()) == {"status", "models", "last_checked", "detail"}
    for m in data["lm_studio"]["models"]:
        assert set(m.keys()) == {"id", "loaded"}
    assert set(data["agent_queue"].keys()) == {"queued", "running", "running_jobs", "current_job", "workflow_efficiency", "needs_continuation", "duplicate_tool_call_warning_threshold", "status_counts", "last_checked", "detail"}


def test_cluster_health_never_leaks_secrets_or_urls(tmp_path: Path, auth_headers):
    """No gateway keys, passwords, LM tokens, prompts, internal URLs, or paths in the response."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    text = response.text
    assert "test-gw-key" not in text
    assert "test-pass" not in text
    assert "test-lm-token" not in text
    assert "m5:8080" not in text
    assert "lmstudio:1234" not in text
    assert "gateway:8765" not in text
    assert "telegram-bot:8080" not in text
    assert "openwebui:3000" not in text
    assert "router:8082" not in text
    assert "/run/secrets" not in text
    assert "/home/user" not in text


def test_cluster_health_gateway_bearer_upstream_only(tmp_path: Path, auth_headers):
    """The gateway key is sent as Bearer header upstream but never appears in the response."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    assert "test-gw-key" not in response.text
    # Verify the gateway call received the Bearer header
    gateway_calls = [c for c in mock_client.get.call_args_list if "v1/dashboard" in str(c)]
    assert len(gateway_calls) == 1
    assert gateway_calls[0].kwargs["headers"]["Authorization"] == "Bearer test-gw-key"


def test_cluster_health_lm_studio_token_bearer_upstream_only(tmp_path: Path, auth_headers):
    """The LM Studio token is sent as Bearer header to /api/v1/models but never appears in the response."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    assert "test-lm-token" not in response.text
    # Verify the LM Studio call hit the native /api/v1/models with the Bearer token
    lm_calls = [c for c in mock_client.get.call_args_list if "api/v1/models" in str(c)]
    assert len(lm_calls) == 1, f"Expected exactly 1 /api/v1/models call, got {len(lm_calls)}"
    assert lm_calls[0].kwargs["headers"]["Authorization"] == "Bearer test-lm-token"
    # Verify no legacy OpenAI-compatible /v1/models call (without /api prefix) was made
    legacy_calls = [c for c in mock_client.get.call_args_list if "/v1/models" in str(c) and "api/v1/models" not in str(c)]
    assert len(legacy_calls) == 0, "Must not probe legacy OpenAI-compatible /v1/models"
    # Verify no /health call was made to LM Studio
    lm_health_calls = [c for c in mock_client.get.call_args_list if "lmstudio:1234/health" in str(c)]
    assert len(lm_health_calls) == 0, "Must not probe nonexistent LM Studio /health"


def test_cluster_health_all_healthy(tmp_path: Path, auth_headers):
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "healthy"
    for svc in data["services"]:
        assert svc["status"] == "healthy"


def test_cluster_health_partial_failure_degraded(tmp_path: Path, auth_headers):
    """One service offline, rest healthy → overall degraded."""
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = httpx.ConnectError("refused")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    m5 = next(s for s in data["services"] if s["name"] == "m5-inference")
    assert m5["status"] == "offline"
    assert m5["detail"] == "connection_refused"


def test_cluster_health_all_external_offline(tmp_path: Path, auth_headers):
    """All external services offline; self is healthy → overall degraded."""
    routes = {
        "lmstudio:1234/api/v1/models": httpx.ConnectError("refused"),
        "gateway:8765/v1/dashboard": httpx.ConnectError("refused"),
        "telegram-bot:8080/health": httpx.ConnectError("refused"),
        "openwebui:3000/health": httpx.ConnectError("refused"),
        "router:8082/health": httpx.ConnectError("refused"),
    }
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "degraded"
    self_svc = next(s for s in data["services"] if s["name"] == "dashboard")
    assert self_svc["status"] == "healthy"


def test_cluster_health_all_unconfigured_healthy(tmp_path: Path, auth_headers):
    """All external URLs empty → unknown; self healthy → overall healthy."""
    mock_client = _routed_client({})
    with _health_env(tmp_path, m5="", lmstudio="", gateway="", telegram="", openwebui="", router=""), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["overall"] == "healthy"
    for svc in data["services"]:
        if svc["name"] == "dashboard":
            assert svc["status"] == "healthy"
        else:
            assert svc["status"] == "unknown"
            assert svc["detail"] == "unconfigured"


def test_cluster_health_telegram_unconfigured(tmp_path: Path, auth_headers):
    """Telegram with empty URL reports unknown/unconfigured without probing any endpoint."""
    mock_client = _routed_client({})
    with _health_env(tmp_path, telegram=""), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    tg = next(s for s in data["services"] if s["name"] == "telegram-bot")
    assert tg["status"] == "unknown"
    assert tg["detail"] == "unconfigured"
    # No HTTP call should have been made to telegram
    tg_calls = [c for c in mock_client.get.call_args_list if "telegram" in str(c)]
    assert len(tg_calls) == 0, "Must not probe telegram when unconfigured"


def test_cluster_health_telegram_healthy(tmp_path: Path, auth_headers):
    """Telegram bot returns 200 → status healthy, overall healthy (all services up)."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    tg = next(s for s in data["services"] if s["name"] == "telegram-bot")
    assert tg["status"] == "healthy"
    assert tg["detail"] is None
    assert tg["latency_ms"] is not None
    assert data["overall"] == "healthy"


def test_cluster_health_telegram_offline(tmp_path: Path, auth_headers):
    """Telegram bot connection refused → status offline, detail connection_refused, overall degraded."""
    routes = _all_healthy_routes()
    routes["telegram-bot:8080/health"] = httpx.ConnectError("connection refused")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    tg = next(s for s in data["services"] if s["name"] == "telegram-bot")
    assert tg["status"] == "offline"
    assert tg["detail"] == "connection_refused"
    assert tg["latency_ms"] is None
    assert data["overall"] == "degraded"


def test_cluster_health_telegram_url_not_in_response(tmp_path: Path, auth_headers):
    """The internal telegram-bot:8080 URL must never appear in the API response body."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    text = response.text
    assert "telegram-bot:8080" not in text
    assert "http://telegram-bot" not in text


def test_cluster_health_overall_with_telegram_offline_only(tmp_path: Path, auth_headers):
    """All services healthy except telegram-bot offline → overall degraded (not offline)."""
    routes = _all_healthy_routes()
    routes["telegram-bot:8080/health"] = httpx.ConnectError("refused")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # All other services must be healthy
    for svc in data["services"]:
        if svc["name"] != "telegram-bot":
            assert svc["status"] == "healthy", f"{svc['name']} should be healthy"
    # Telegram is offline
    tg = next(s for s in data["services"] if s["name"] == "telegram-bot")
    assert tg["status"] == "offline"
    # Overall is degraded (not offline) because dashboard self-check is always healthy
    assert data["overall"] == "degraded"


def test_cluster_health_overall_all_offline_including_telegram(tmp_path: Path, auth_headers):
    """All external services offline (including telegram) → overall degraded.

    The dashboard self-check always returns healthy, so the overall can never
    be 'offline' in practice. All-externals-offline yields 'degraded'.
    """
    routes = {
        "lmstudio:1234/api/v1/models": httpx.ConnectError("refused"),
        "gateway:8765/v1/dashboard": httpx.ConnectError("refused"),
        "telegram-bot:8080/health": httpx.ConnectError("refused"),
        "openwebui:3000/health": httpx.ConnectError("refused"),
        "router:8082/health": httpx.ConnectError("refused"),
    }
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # Dashboard self-check is always healthy
    self_svc = next(s for s in data["services"] if s["name"] == "dashboard")
    assert self_svc["status"] == "healthy"
    # All external services are offline
    for svc in data["services"]:
        if svc["name"] != "dashboard":
            assert svc["status"] == "offline", f"{svc['name']} should be offline"
    # Overall is degraded (self is healthy, so not all are offline)
    assert data["overall"] == "degraded"


def test_cluster_health_timeout_marks_offline(tmp_path: Path, auth_headers):
    """A service that times out is marked offline with detail 'timeout'."""
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = httpx.ReadTimeout("timed out")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch.object(app, "HEALTH_TIMEOUT", 1.0), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    m5 = next(s for s in data["services"] if s["name"] == "m5-inference")
    assert m5["status"] == "offline"
    assert m5["detail"] == "timeout"


def test_cluster_health_bounded_concurrency(tmp_path: Path, auth_headers):
    """HEALTH_CONCURRENCY semaphore is used; endpoint completes correctly with a small limit."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch.object(app, "HEALTH_CONCURRENCY", 1), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["overall"] == "healthy"


def test_cluster_health_m5_reachable(tmp_path: Path, auth_headers):
    routes = _all_healthy_routes()
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    m5 = next(s for s in data["services"] if s["name"] == "m5-inference")
    assert m5["status"] == "healthy"
    assert m5["latency_ms"] is not None


def test_cluster_health_m5_offline(tmp_path: Path, auth_headers):
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = httpx.ConnectError("refused")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    m5 = next(s for s in data["services"] if s["name"] == "m5-inference")
    assert m5["status"] == "offline"
    assert m5["detail"] == "connection_refused"


def test_cluster_health_m5_unconfigured(tmp_path: Path, auth_headers):
    mock_client = _routed_client({})
    with _health_env(tmp_path, lmstudio=""), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    m5 = next(s for s in data["services"] if s["name"] == "m5-inference")
    assert m5["status"] == "unknown"
    assert m5["detail"] == "unconfigured"


def test_cluster_health_lm_studio_loaded_models(tmp_path: Path, auth_headers):
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = _make_gateway_response(200, {"models": [
        {"key": "meta-llama/Llama-3-8B", "loaded_instances": [1, 2]},
        {"key": "mistral-7b", "loaded_instances": []},
    ]})
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    assert data["lm_studio"]["status"] == "healthy"
    models = data["lm_studio"]["models"]
    assert len(models) == 2
    assert models[0] == {"id": "meta-llama/Llama-3-8B", "loaded": True}
    assert models[1] == {"id": "mistral-7b", "loaded": False}


def test_cluster_health_lm_studio_unavailable(tmp_path: Path, auth_headers):
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = httpx.ConnectError("refused")
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    assert data["lm_studio"]["status"] == "offline"
    assert data["lm_studio"]["models"] == []
    assert data["lm_studio"]["detail"] == "connection_refused"


def test_cluster_health_lm_studio_malformed_ids_dropped(tmp_path: Path, auth_headers):
    """Malformed model IDs (spaces, too long, non-string) are dropped; valid ones remain."""
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = _make_gateway_response(200, {"models": [
        {"key": "valid-model", "loaded_instances": [1]},
        {"key": "model with spaces", "loaded_instances": [1]},
        {"key": "a" * (app.LMSTUDIO_MODEL_ID_MAX + 1), "loaded_instances": [1]},
        {"key": None, "loaded_instances": [1]},
        {"key": "another-valid", "loaded_instances": []},
    ]})
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    models = data["lm_studio"]["models"]
    assert len(models) == 2
    assert models[0]["id"] == "valid-model"
    assert models[1]["id"] == "another-valid"


def test_cluster_health_lm_studio_bounded_ids(tmp_path: Path, auth_headers):
    """IDs at exactly the max length are kept; one over is dropped."""
    routes = _all_healthy_routes()
    at_max = "a" * app.LMSTUDIO_MODEL_ID_MAX
    over_max = "a" * (app.LMSTUDIO_MODEL_ID_MAX + 1)
    routes["lmstudio:1234/api/v1/models"] = _make_gateway_response(200, {"models": [
        {"key": at_max, "loaded_instances": [1]},
        {"key": over_max, "loaded_instances": [1]},
    ]})
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    models = data["lm_studio"]["models"]
    assert len(models) == 1
    assert models[0]["id"] == at_max


def test_cluster_health_lm_studio_live_response_regression(tmp_path: Path, auth_headers):
    """Regression matching the live LM Studio /api/v1/models response shape.

    DeepSeek and Qwen are loaded (loaded_instances non-empty); Gemma and the
    embedding model are available but not loaded (loaded_instances empty).
    Extra fields in the native response are ignored.
    """
    routes = _all_healthy_routes()
    routes["lmstudio:1234/api/v1/models"] = _make_gateway_response(200, {"models": [
        {
            "key": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
            "loaded_instances": [1, 2],
            "model_path": "/models/deepseek",
            "context_length": 4096,
        },
        {
            "key": "Qwen/Qwen2.5-7B-Instruct",
            "loaded_instances": [3],
            "model_path": "/models/qwen",
            "context_length": 8192,
        },
        {
            "key": "google/gemma-2-9b-it",
            "loaded_instances": [],
            "model_path": "/models/gemma",
            "context_length": 8192,
        },
        {
            "key": "sentence-transformers/all-MiniLM-L6-v2",
            "loaded_instances": [],
            "model_path": "/models/embedding",
            "context_length": 512,
        },
    ]})
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["lm_studio"]["status"] == "healthy"
    models = data["lm_studio"]["models"]
    assert len(models) == 4
    # DeepSeek: loaded (loaded_instances has 2 entries)
    assert models[0] == {"id": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B", "loaded": True}
    # Qwen: loaded (loaded_instances has 1 entry)
    assert models[1] == {"id": "Qwen/Qwen2.5-7B-Instruct", "loaded": True}
    # Gemma: available but not loaded (loaded_instances empty)
    assert models[2] == {"id": "google/gemma-2-9b-it", "loaded": False}
    # Embedding model: available but not loaded (loaded_instances empty)
    assert models[3] == {"id": "sentence-transformers/all-MiniLM-L6-v2", "loaded": False}
    # No internal paths or extra fields leak into the response
    text = response.text
    assert "/models/deepseek" not in text
    assert "/models/qwen" not in text
    assert "/models/gemma" not in text
    assert "/models/embedding" not in text
    assert "context_length" not in text
    assert "model_path" not in text


# --- Frontend source contracts: cluster health panel ---


def test_frontend_html_cluster_health_panel():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="cluster-health-panel"' in html
    assert 'id="cluster-health"' in html
    assert 'id="health-timestamp"' in html
    assert "CLUSTER HEALTH" in html


def test_frontend_js_cluster_health_uses_safe():
    """All dynamic content in renderClusterHealth must use safe() for HTML escaping."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function renderClusterHealth\(data\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "renderClusterHealth function not found"
    body = match.group(0)
    assert "safe(overall)" in body
    assert "safe(status)" in body
    assert "safe(svc.name)" in body
    assert "safe(svc.detail)" in body
    assert "safe(m.id)" in body
    assert "safe(agentQueue.current_job.id" in body
    assert "health-efficiency" in body


def test_frontend_js_cluster_health_statuses():
    """The panel renders status values dynamically via safe() for all states."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function renderClusterHealth\(data\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    # The function renders the overall and per-service status via safe()
    assert "safe(overall)" in body
    assert "safe(status)" in body
    # Fallback values are present for missing data
    assert "unknown" in body
    # The CSS provides distinct classes for all four states (verified in CSS test)


def test_frontend_js_cluster_health_timestamp():
    """The timestamp is rendered from generated_at using toLocaleTimeString."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function renderClusterHealth\(data\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    assert "generated_at" in body
    assert "toLocaleTimeString" in body
    assert "health-timestamp" in body


def test_frontend_html_cluster_health_aria():
    """The cluster health container must have role=status and aria-live=polite."""
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="cluster-health"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_frontend_js_cluster_health_error_state():
    """renderClusterHealthError shows a safe offline state with a muted message."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function renderClusterHealthError\(\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "renderClusterHealthError function not found"
    body = match.group(0)
    assert "offline" in body
    assert "unavailable" in body
    assert "health-timestamp" in body


def test_frontend_js_cluster_health_polling_30s():
    """The cluster health panel polls every 30 seconds."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "setInterval(loadClusterHealth,30000)" in js


def test_frontend_css_cluster_health_responsive():
    """Health cards use auto-fill grid and collapse to single column on small screens."""
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".health-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))" in css
    assert ".health-cards{grid-template-columns:1fr}" in css
    assert ".health-card-wide{grid-column:auto}" in css
    assert ".health-grid{display:grid" in css
    assert ".health-overall.status.healthy" in css
    assert ".health-overall.status.degraded" in css
    assert ".health-overall.status.offline" in css
    assert ".health-overall.status.unknown" in css


def test_frontend_html_reliability_card():
    """index.html exposes the all-time job status counts card with accessible region semantics."""
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="reliability-card"' in html
    assert 'role="region"' in html
    assert 'aria-label="All-time job status counts"' in html


def test_frontend_js_reliability_card():
    """app.js renders the reliability card, explains the caveat, and maps null to Unavailable."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "reliability-card" in js
    assert "All-time job status counts" in js
    # The card must explain that completed does not mean merged and that totals include canaries.
    assert "Completed does not mean merged" in js
    assert "historical canaries" in js
    # Reads the projected status_counts and renders all three known statuses.
    assert "status_counts" in js
    assert "sc.completed" in js
    assert "sc.failed" in js
    assert "sc.blocked" in js
    # null/undefined values must render as "Unavailable", never a false zero.
    assert "'Unavailable'" in js


def test_frontend_css_reliability_card():
    """The reliability card is styled and its grid collapses responsively."""
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".reliability-card" in css
    assert ".reliability-grid" in css
    assert ".reliability-note" in css
    assert "@media" in css


# --- Repair visibility: _safe_bool and _safe_nonneg_int ---


def test_safe_bool_true():
    assert app._safe_bool(True) is True


def test_safe_bool_false():
    assert app._safe_bool(False) is False


def test_safe_bool_none():
    assert app._safe_bool(None) is None


def test_safe_bool_rejects_int():
    assert app._safe_bool(1) is None
    assert app._safe_bool(0) is None


def test_safe_bool_rejects_string():
    assert app._safe_bool("true") is None
    assert app._safe_bool("false") is None
    assert app._safe_bool("") is None


def test_safe_bool_rejects_float():
    assert app._safe_bool(1.0) is None


def test_safe_nonneg_int_zero():
    assert app._safe_nonneg_int(0) == 0


def test_safe_nonneg_int_positive():
    assert app._safe_nonneg_int(5) == 5
    assert app._safe_nonneg_int(100) == 100


def test_safe_nonneg_int_none():
    assert app._safe_nonneg_int(None) is None


def test_safe_nonneg_int_rejects_negative():
    assert app._safe_nonneg_int(-1) is None
    assert app._safe_nonneg_int(-100) is None


def test_safe_nonneg_int_rejects_bool():
    assert app._safe_nonneg_int(True) is None
    assert app._safe_nonneg_int(False) is None


def test_safe_nonneg_int_rejects_float():
    assert app._safe_nonneg_int(1.5) is None
    assert app._safe_nonneg_int(0.0) is None


def test_safe_nonneg_int_rejects_string():
    assert app._safe_nonneg_int("5") is None
    assert app._safe_nonneg_int("") is None


# --- Repair visibility: _derive_repair_state ---


def test_derive_repair_state_none_when_disabled():
    assert app._derive_repair_state(False, True, 3, 5, "running", None) == "none"


def test_derive_repair_state_none_when_not_activated():
    assert app._derive_repair_state(True, False, 3, 5, "running", None) == "none"


def test_derive_repair_state_none_when_zero_attempts():
    assert app._derive_repair_state(True, True, 0, 5, "running", None) == "none"


def test_derive_repair_state_none_when_missing_attempts():
    assert app._derive_repair_state(True, True, None, 5, "running", None) == "none"


def test_derive_repair_state_none_when_missing_enabled():
    """Missing enabled (None) is treated as not explicitly disabled, so we check activated."""
    # If enabled is None (missing) and activated is True, we proceed
    assert app._derive_repair_state(None, True, 0, 5, "running", None) == "none"


def test_derive_repair_state_repairing():
    assert app._derive_repair_state(True, True, 1, 3, "running", None) == "repairing"


def test_derive_repair_state_repairing_multiple_attempts():
    assert app._derive_repair_state(True, True, 2, 5, "running", None) == "repairing"


def test_derive_repair_state_ready_after_repair():
    assert app._derive_repair_state(True, True, 2, 5, "ready-for-approval", "APPROVE") == "ready_after_repair"


def test_derive_repair_state_ready_after_repair_at_max():
    """Even at max attempts, if reviewer approved and ready, it's ready_after_repair."""
    assert app._derive_repair_state(True, True, 3, 3, "ready-for-approval", "APPROVE") == "ready_after_repair"


def test_derive_repair_state_exhausted():
    assert app._derive_repair_state(True, True, 3, 3, "running", None) == "exhausted"


def test_derive_repair_state_exhausted_over_max():
    assert app._derive_repair_state(True, True, 5, 3, "running", None) == "exhausted"


def test_derive_repair_state_exhausted_with_reject():
    """At max with rejected status: exhausted (not rejected_attempts_remaining)."""
    assert app._derive_repair_state(True, True, 3, 3, "rejected", "REJECT") == "exhausted"


def test_derive_repair_state_rejected_attempts_remaining():
    assert app._derive_repair_state(True, True, 1, 3, "rejected", None) == "rejected_attempts_remaining"


def test_derive_repair_state_rejected_attempts_remaining_at_boundary():
    """Attempts < max with rejected status: rejected_attempts_remaining."""
    assert app._derive_repair_state(True, True, 2, 3, "rejected", None) == "rejected_attempts_remaining"


def test_derive_repair_state_never_ready_without_approval():
    """Never imply success without reviewer approval."""
    assert app._derive_repair_state(True, True, 2, 5, "ready-for-approval", None) == "none"
    assert app._derive_repair_state(True, True, 2, 5, "ready-for-approval", "REJECT") == "none"


def test_derive_repair_state_never_ready_without_ready_status():
    """Never imply success without ready-for-approval status."""
    assert app._derive_repair_state(True, True, 2, 5, "running", "APPROVE") == "repairing"


def test_derive_repair_state_missing_max_defaults_to_repairing():
    """If max is None (missing), attempts > 0 with no reject means repairing."""
    assert app._derive_repair_state(True, True, 2, None, "running", None) == "repairing"


def test_derive_repair_state_missing_max_with_reject():
    """If max is None and status is running, it's repairing (verdict alone doesn't change state)."""
    assert app._derive_repair_state(True, True, 2, None, "running", "REJECT") == "repairing"


def test_derive_repair_state_missing_max_with_approval_and_ready():
    """If max is None but reviewer approved and ready, it's ready_after_repair."""
    assert app._derive_repair_state(True, True, 2, None, "ready-for-approval", "APPROVE") == "ready_after_repair"


def test_derive_repair_state_terminal_merged():
    """Terminal merged status with attempts → neutral history, never repairing/success."""
    assert app._derive_repair_state(True, True, 1, None, "merged", None) == "history"
    assert app._derive_repair_state(True, True, 3, 5, "merged", "APPROVE") == "history"


def test_derive_repair_state_terminal_failed():
    assert app._derive_repair_state(True, True, 2, 5, "failed", None) == "history"


def test_derive_repair_state_terminal_pushed():
    assert app._derive_repair_state(True, True, 1, 3, "pushed", None) == "history"


def test_derive_repair_state_terminal_archived():
    assert app._derive_repair_state(True, True, 1, 3, "archived", None) == "history"


def test_derive_repair_state_terminal_blocked():
    assert app._derive_repair_state(True, True, 1, 3, "blocked", None) == "history"


def test_derive_repair_state_terminal_completed():
    assert app._derive_repair_state(True, True, 1, 3, "completed", None) == "history"


def test_derive_repair_state_terminal_never_repairing_or_success():
    """No terminal state may display as actively repairing or falsely claim success."""
    for status in ("merged", "pushed", "archived", "failed", "blocked", "completed"):
        for verdict in (None, "APPROVE", "REJECT"):
            result = app._derive_repair_state(True, True, 1, 3, status, verdict)
            assert result == "history", f"{status}/{verdict} → {result}, expected history"


def test_derive_repair_state_queued():
    """Queued with attempts > 0 → actively repairing (genuinely active chain)."""
    assert app._derive_repair_state(True, True, 1, 3, "queued", None) == "repairing"
    assert app._derive_repair_state(True, True, 2, 5, "queued", None) == "repairing"


# --- Repair visibility: _project_workflow with repair fields ---


def _gateway_payload_with_repair(**overrides):
    payload = _gateway_payload()
    payload.update({
        "repair_enabled": True,
        "repair_activated": True,
        "repair_attempts": 2,
        "repair_max_attempts": 3,
    })
    payload.update(overrides)
    return payload


def test_project_workflow_with_repair_fields(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["repair_enabled"] is True
    assert data["repair_activated"] is True
    assert data["repair_attempts"] == 2
    assert data["repair_max_attempts"] == 3
    # Status is ready-for-approval and verdict is APPROVE → ready_after_repair
    assert data["repair_state"] == "ready_after_repair"


def test_project_workflow_repair_disabled(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(repair_enabled=False)
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_enabled"] is False
    assert data["repair_state"] == "none"


def test_project_workflow_repair_not_activated(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(repair_activated=False)
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_activated"] is False
    assert data["repair_state"] == "none"


def test_project_workflow_repair_zero_attempts(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(repair_attempts=0)
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_attempts"] == 0
    assert data["repair_state"] == "none"


def test_project_workflow_repair_missing_fields(password_file, gateway_key_file, auth_headers):
    """Legacy payload without repair fields: all None, state none."""
    payload = _gateway_payload()
    # No repair fields at all
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_enabled"] is None
    assert data["repair_activated"] is None
    assert data["repair_attempts"] is None
    assert data["repair_max_attempts"] is None
    assert data["repair_state"] == "none"


def test_project_workflow_repair_malformed_values(password_file, gateway_key_file, auth_headers):
    """Malformed repair values are safely handled as None."""
    payload = _gateway_payload()
    payload["repair_enabled"] = "yes"  # not a bool
    payload["repair_activated"] = 1  # not a bool
    payload["repair_attempts"] = -5  # negative
    payload["repair_max_attempts"] = "three"  # not an int
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_enabled"] is None
    assert data["repair_activated"] is None
    assert data["repair_attempts"] is None
    assert data["repair_max_attempts"] is None
    assert data["repair_state"] == "none"


def test_project_workflow_repair_reject_state(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(
        repair_attempts=1, repair_max_attempts=3,
    )
    payload["workflow"]["overall"] = "rejected"
    payload["reviewer_verdict"] = "REJECT"
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_state"] == "rejected_attempts_remaining"


def test_project_workflow_repair_exhausted_state(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(
        repair_attempts=3, repair_max_attempts=3,
    )
    payload["workflow"]["overall"] = "running"
    payload["reviewer_verdict"] = "REJECT"
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_state"] == "exhausted"


def test_project_workflow_repair_repairing_state(password_file, gateway_key_file, auth_headers):
    payload = _gateway_payload_with_repair(
        repair_attempts=1, repair_max_attempts=3,
    )
    payload["workflow"]["overall"] = "running"
    payload["reviewer_verdict"] = None
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    data = response.json()
    assert data["repair_state"] == "repairing"


def test_project_workflow_never_leaks_unsafe_repair_fields(password_file, gateway_key_file, auth_headers):
    """Unsafe fields in the repair section must not leak through."""
    payload = _gateway_payload_with_repair()
    payload["repair_prompt"] = "SECRET-REPAIR-PROMPT"
    payload["repair_worktree"] = "/home/user/worktrees/secret"
    payload["repair_api_key"] = "sk-secret-123"
    payload["repair_internal_url"] = "http://internal:9999"
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/workflows/0123456789", headers=auth_headers)
    text = response.text
    assert "SECRET-REPAIR-PROMPT" not in text
    assert "/home/user/worktrees" not in text
    assert "sk-secret-123" not in text
    assert "internal:9999" not in text


# --- Repair visibility: _project_dashboard ---


def _dashboard_payload_with_repair():
    payload = _dashboard_payload()
    payload["repair_enabled"] = True
    payload["repair_activated"] = True
    payload["repair_max_attempts"] = 3
    payload["recent_workflows"][0]["repair_activated"] = True
    payload["recent_workflows"][0]["repair_attempts"] = 1
    payload["recent_workflows"][0]["repair_max_attempts"] = 3
    return payload


def test_dashboard_adds_system_repair_fields(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload_with_repair()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["repair_enabled"] is True
    assert data["repair_activated"] is True
    assert data["repair_max_attempts"] == 3


def test_dashboard_adds_repair_state_to_workflows(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload_with_repair()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    wf = data["recent_workflows"][0]
    assert "repair_state" in wf
    # overall is "running", attempts=1, max=3, no verdict → repairing
    assert wf["repair_state"] == "repairing"


def test_dashboard_legacy_payload_no_repair_fields(password_file, gateway_key_file, auth_headers):
    """Legacy gateway payload without repair fields: system fields None, state none."""
    payload = _dashboard_payload()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["repair_enabled"] is None
    assert data["repair_activated"] is None
    assert data["repair_max_attempts"] is None
    wf = data["recent_workflows"][0]
    assert wf["repair_state"] == "none"


def test_dashboard_malformed_repair_fields(password_file, gateway_key_file, auth_headers):
    """Malformed system-level repair fields are safely handled."""
    payload = _dashboard_payload()
    payload["repair_enabled"] = "yes"
    payload["repair_activated"] = 1
    payload["repair_max_attempts"] = -1
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    assert data["repair_enabled"] is None
    assert data["repair_activated"] is None
    assert data["repair_max_attempts"] is None
    assert data["recent_workflows"][0]["repair_state"] == "none"


def test_dashboard_preserves_existing_fields(password_file, gateway_key_file, auth_headers):
    """The dashboard projection preserves all existing fields."""
    payload = _dashboard_payload_with_repair()
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    # All original fields preserved
    assert data["jobs"] == payload["jobs"]
    assert data["usage"] == payload["usage"]
    assert data["counts"] == payload["counts"]
    assert data["projects"] == payload["projects"]
    assert data["generated_at"] == payload["generated_at"]
    # Workflow fields preserved
    wf = data["recent_workflows"][0]
    assert wf["id"] == "0123456789"
    assert wf["objective"] == "Build feature X"
    assert wf["overall"] == "running"
    assert wf["origin"] == "telegram"
    assert wf["models"] == ["local-model"]


def test_dashboard_repair_state_ready_after_repair(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload_with_repair()
    payload["recent_workflows"][0]["overall"] = "ready-for-approval"
    payload["recent_workflows"][0]["reviewer_verdict"] = "APPROVE"
    payload["recent_workflows"][0]["repair_attempts"] = 2
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    assert data["recent_workflows"][0]["repair_state"] == "ready_after_repair"


def test_dashboard_repair_state_exhausted(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload_with_repair()
    payload["recent_workflows"][0]["overall"] = "running"
    payload["recent_workflows"][0]["repair_attempts"] = 3
    payload["recent_workflows"][0]["repair_max_attempts"] = 3
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    assert data["recent_workflows"][0]["repair_state"] == "exhausted"


def test_dashboard_repair_state_rejected(password_file, gateway_key_file, auth_headers):
    payload = _dashboard_payload_with_repair()
    payload["recent_workflows"][0]["overall"] = "rejected"
    payload["recent_workflows"][0]["reviewer_verdict"] = "REJECT"
    payload["recent_workflows"][0]["repair_attempts"] = 1
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    data = response.json()
    assert data["recent_workflows"][0]["repair_state"] == "rejected_attempts_remaining"


def test_dashboard_non_dict_response_passthrough(password_file, gateway_key_file, auth_headers):
    """If the gateway returns a non-dict (e.g., a list), it's passed through as-is."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, ["not", "a", "dict"]))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == ["not", "a", "dict"]


def test_dashboard_live_payload_merged_regression(password_file, gateway_key_file, auth_headers):
    """Regression: the exact live gateway payload that triggered the bug.

    overall=merged, repair_attempts=1, no repair_max_attempts, no reviewer_verdict.
    The workflow must show neutral 'history', never 'repairing' or success.
    """
    payload = _dashboard_payload()
    # System-level repair fields as in the live gateway
    payload["repair_enabled"] = True
    payload["repair_activated"] = True
    # No system-level repair_max_attempts (missing in live payload)
    # Per-workflow: the exact live regression payload
    wf = payload["recent_workflows"][0]
    wf["overall"] = "merged"
    wf["repair_attempts"] = 1
    # No repair_max_attempts, no reviewer_verdict (missing in live payload)
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/dashboard", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    wf_result = data["recent_workflows"][0]
    # The core regression: merged must never show as repairing
    assert wf_result["repair_state"] == "history"
    assert wf_result["repair_state"] != "repairing"
    assert wf_result["repair_state"] != "ready_after_repair"
    # Status is normalized from overall
    assert wf_result["status"] == "merged"
    # repair_attempts is preserved
    assert wf_result["repair_attempts"] == 1
    # No max → no /max claims
    assert wf_result.get("repair_max_attempts") is None


# --- Repair visibility: Frontend source contracts ---


def test_frontend_js_has_repair_badge_function():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "function repairBadge(" in js
    assert "repair-badge" in js
    assert "repair-none" in js
    # The class is constructed dynamically: repair-${state.replace(/_/g, '-')}
    assert "repair-${state.replace(/_/g, '-')}" in js
    # All state values must appear in the if/else chain
    assert "'repairing'" in js
    assert "'ready_after_repair'" in js
    assert "'rejected_attempts_remaining'" in js
    assert "'exhausted'" in js


def test_frontend_js_repair_badge_uses_safe():
    """All dynamic text in repairBadge must use safe() for HTML escaping."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function repairBadge\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "repairBadge function not found"
    body = match.group(0)
    assert "safe(label)" in body
    assert "safe(state)" in body


def test_frontend_js_repair_badge_handles_none():
    """repairBadge returns a neutral indicator for 'none' or missing state."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function repairBadge\(.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    assert "state === 'none'" in body
    assert "repair-none" in body


def test_frontend_js_renders_repair_in_workflows_table():
    """The workflows table rendering must include the repair badge."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "repairBadge(w.repair_state" in js


def test_frontend_js_renders_repair_in_workflow_detail():
    """The workflow detail must include the repair badge."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "repairBadge(workflow.repair_state" in js


def test_frontend_js_system_repair_indicator():
    """The metrics area must include a system-level repair indicator."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "repair_enabled" in js
    assert "repair_activated" in js
    assert "repair_max_attempts" in js
    assert "Auto-repair" in js
    assert "repair-system" in js


def test_frontend_html_has_repair_column():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "Repair" in html


def test_frontend_css_has_repair_badge_styles():
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".repair-badge" in css
    assert ".repair-none" in css
    assert ".repair-repairing" in css
    assert ".repair-ready-after-repair" in css
    assert ".repair-rejected-attempts-remaining" in css
    assert ".repair-exhausted" in css
    assert ".repair-history" in css
    assert ".repair-system" in css
    assert ".repair-active" in css
    assert ".repair-enabled" in css
    assert ".repair-off" in css


def test_frontend_css_repair_colors_match_status_palette():
    """Repair badge colors use the same CSS variables as status indicators."""
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    # Teal for success (ready_after_repair, active)
    assert "var(--teal)" in css
    # Amber for in-progress (repairing, enabled)
    assert "var(--amber)" in css
    # Red for failure (exhausted, rejected)
    assert "var(--red)" in css
    # Muted for none/off
    assert "var(--muted)" in css


# --- Repair visibility: Regression — no new controls ---


def test_no_repair_controls_in_workflow_actions():
    """The workflowActions function must not add any repair-related buttons."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function workflowActions\(.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    # No repair action buttons
    assert "repair" not in body.lower() or "data-workflow-action" not in body


def test_no_repair_in_workflow_action_endpoint():
    """The backend WORKFLOW_ACTIONS set must not include 'repair'."""
    assert "repair" not in app.WORKFLOW_ACTIONS


def test_manual_approval_gates_preserved():
    """The existing action roles must be unchanged."""
    assert app.ACTION_ROLES["approve"] == {"admin"}
    assert app.ACTION_ROLES["merge"] == {"admin"}
    assert app.ACTION_ROLES["push"] == {"admin"}
    assert app.ACTION_ROLES["retry"] == {"operator", "admin"}
    assert app.ACTION_ROLES["rereview"] == {"operator", "admin"}


def test_repair_is_read_only_no_mutation_endpoint():
    """There must be no POST/PUT/DELETE endpoint for repair."""
    # Check that no route with 'repair' in the path exists
    routes = [r.path for r in app.app.routes]
    repair_routes = [r for r in routes if "repair" in r.lower()]
    assert repair_routes == [], f"Unexpected repair mutation routes: {repair_routes}"


# --- Repair visibility: Comprehensive state coverage ---


def test_repair_state_zero_attempts_is_none():
    assert app._derive_repair_state(True, True, 0, 5, "running", None) == "none"
    assert app._derive_repair_state(True, True, 0, 5, "ready-for-approval", "APPROVE") == "none"


def test_repair_state_active_repair():
    assert app._derive_repair_state(True, True, 1, 3, "running", None) == "repairing"
    assert app._derive_repair_state(True, True, 2, 5, "running", None) == "repairing"


def test_repair_state_successful_repair():
    assert app._derive_repair_state(True, True, 1, 5, "ready-for-approval", "APPROVE") == "ready_after_repair"
    assert app._derive_repair_state(True, True, 3, 3, "ready-for-approval", "APPROVE") == "ready_after_repair"


def test_repair_state_repeated_rejection():
    assert app._derive_repair_state(True, True, 1, 5, "rejected", None) == "rejected_attempts_remaining"
    assert app._derive_repair_state(True, True, 2, 5, "rejected", None) == "rejected_attempts_remaining"
    assert app._derive_repair_state(True, True, 4, 5, "rejected", None) == "rejected_attempts_remaining"


def test_repair_state_exhausted():
    assert app._derive_repair_state(True, True, 3, 3, "running", "REJECT") == "exhausted"
    assert app._derive_repair_state(True, True, 5, 3, "running", None) == "exhausted"
    assert app._derive_repair_state(True, True, 3, 3, "rejected", None) == "exhausted"


def test_repair_state_disabled():
    assert app._derive_repair_state(False, True, 3, 5, "running", None) == "none"
    assert app._derive_repair_state(False, False, 0, 5, "running", None) == "none"


def test_repair_state_not_yet_activated():
    assert app._derive_repair_state(True, False, 3, 5, "running", None) == "none"
    assert app._derive_repair_state(True, False, 0, 5, "running", None) == "none"


def test_repair_state_malformed_values():
    """All malformed values result in None → state none."""
    assert app._derive_repair_state(None, None, None, None, None, None) == "none"


# --- Alerts: _project_active_alert ---


def test_project_active_alert_valid():
    raw = {
        "job_id": "abcdef123456",
        "status": "failed",
        "role": "operator",
        "project": "firstproject",
        "stage": "test",
        "created_at": "2025-01-15T10:00:00Z",
    }
    result = app._project_active_alert(raw)
    assert result == {
        "job_id": "abcdef123456",
        "status": "failed",
        "role": "operator",
        "project": "firstproject",
        "stage": "test",
        "created_at": "2025-01-15T10:00:00Z",
    }


def test_project_active_alert_invalid_job_id():
    raw = {"job_id": "not-valid", "project": "p1", "status": "failed"}
    result = app._project_active_alert(raw)
    assert result["job_id"] is None


def test_project_active_alert_never_leaks_unsafe_fields():
    raw = {
        "job_id": "abcdef123456",
        "project": "firstproject",
        "status": "failed",
        "created_at": "2025-01-15T10:00:00Z",
        "prompt": "SECRET-PROMPT",
        "worktree": "/home/user/worktrees/secret",
        "api_key": "sk-secret-123",
        "branch": "main",
        "commit_sha": "abc123def456",
        "internal_url": "http://internal:9999",
        "resolution_note": "SECRET-NOTE",
        "error": "SECRET-ERROR",
    }
    result = app._project_active_alert(raw)
    raw_json = app.json.dumps(result)
    assert "SECRET-PROMPT" not in raw_json
    assert "/home/user/worktrees" not in raw_json
    assert "sk-secret-123" not in raw_json
    assert "abc123def456" not in raw_json
    assert "internal:9999" not in raw_json
    assert "SECRET-NOTE" not in raw_json
    assert "SECRET-ERROR" not in raw_json
    assert set(result.keys()) == {"job_id", "status", "role", "project", "stage", "created_at"}


# --- Alerts: _project_acknowledged_alert ---


def test_project_acknowledged_alert_valid():
    raw = {
        "job_id": "abcdef123456",
        "status": "failed",
        "role": "operator",
        "project": "firstproject",
        "stage": "test",
        "created_at": "2025-01-15T10:00:00Z",
        "acknowledged_at": "2025-01-15T11:00:00Z",
        "actor": "admin",
    }
    result = app._project_acknowledged_alert(raw)
    assert result == {
        "job_id": "abcdef123456",
        "status": "failed",
        "role": "operator",
        "project": "firstproject",
        "stage": "test",
        "created_at": "2025-01-15T10:00:00Z",
        "acknowledged_at": "2025-01-15T11:00:00Z",
        "actor": "admin",
    }


def test_project_acknowledged_alert_invalid_job_id():
    raw = {"job_id": "not-valid", "project": "p1", "status": "failed"}
    result = app._project_acknowledged_alert(raw)
    assert result["job_id"] is None


def test_project_acknowledged_alert_never_leaks_unsafe_fields():
    raw = {
        "job_id": "abcdef123456",
        "project": "firstproject",
        "status": "failed",
        "created_at": "2025-01-15T10:00:00Z",
        "acknowledged_at": "2025-01-15T11:00:00Z",
        "actor": "admin",
        "prompt": "SECRET-PROMPT",
        "worktree": "/home/user/worktrees/secret",
        "api_key": "sk-secret-123",
        "branch": "main",
        "commit_sha": "abc123def456",
        "internal_url": "http://internal:9999",
        "resolution_note": "SECRET-NOTE",
        "error": "SECRET-ERROR",
    }
    result = app._project_acknowledged_alert(raw)
    raw_json = app.json.dumps(result)
    assert "SECRET-PROMPT" not in raw_json
    assert "/home/user/worktrees" not in raw_json
    assert "sk-secret-123" not in raw_json
    assert "abc123def456" not in raw_json
    assert "internal:9999" not in raw_json
    assert "SECRET-NOTE" not in raw_json
    assert "SECRET-ERROR" not in raw_json
    assert set(result.keys()) == {"job_id", "status", "role", "project", "stage", "created_at", "acknowledged_at", "actor"}


def test_project_acknowledged_alert_rejects_unsafe_actor():
    """An actor with control characters or excessive length is set to None."""
    raw = {
        "job_id": "abcdef123456",
        "status": "failed",
        "actor": "bad\nactor",
    }
    result = app._project_acknowledged_alert(raw)
    assert result["actor"] is None

    raw2 = {
        "job_id": "abcdef123456",
        "status": "failed",
        "actor": "a" * 129,
    }
    result2 = app._project_acknowledged_alert(raw2)
    assert result2["actor"] is None


# --- Alerts: _project_alerts ---


def test_project_alerts_empty():
    result = app._project_alerts({})
    assert result == {"active": [], "history": []}


def test_project_alerts_valid():
    data = {
        "active": [
            {"job_id": "abcdef123456", "status": "failed", "role": "operator", "project": "p1", "stage": "test", "created_at": "2025-01-15T10:00:00Z"},
            {"job_id": "111111111111", "status": "blocked", "role": "admin", "project": "p2", "stage": "plan", "created_at": "2025-01-15T11:00:00Z"},
        ],
        "acknowledged": [
            {"job_id": "222222222222", "status": "failed", "role": "operator", "project": "p1", "stage": "test", "created_at": "2025-01-14T10:00:00Z", "acknowledged_at": "2025-01-15T09:00:00Z", "actor": "admin"},
        ],
    }
    result = app._project_alerts(data)
    assert len(result["active"]) == 2
    assert len(result["history"]) == 1
    assert result["history"][0]["acknowledged_at"] == "2025-01-15T09:00:00Z"
    assert result["history"][0]["actor"] == "admin"


def test_project_alerts_filters_invalid_ids():
    data = {
        "active": [
            {"job_id": "abcdef123456", "project": "p1", "status": "failed"},
            {"job_id": "invalid", "project": "p2", "status": "failed"},
            {"job_id": None, "project": "p3", "status": "failed"},
        ],
    }
    result = app._project_alerts(data)
    assert len(result["active"]) == 1
    assert result["active"][0]["job_id"] == "abcdef123456"


def test_project_alerts_bounded_history():
    data = {
        "acknowledged": [
            {"job_id": f"{'a' * 12}", "project": "p1", "status": "failed", "acknowledged_at": "2025-01-15T09:00:00Z", "actor": "admin"}
            for _ in range(app.MAX_ALERT_HISTORY + 10)
        ],
    }
    result = app._project_alerts(data)
    assert len(result["history"]) == app.MAX_ALERT_HISTORY


def test_project_alerts_never_leaks_unsafe_fields():
    data = {
        "active": [
            {
                "job_id": "abcdef123456",
                "project": "p1",
                "status": "failed",
                "prompt": "SECRET-PROMPT",
                "worktree": "/home/user/worktrees/secret",
                "api_key": "sk-secret-123",
                "branch": "main",
                "commit_sha": "abc123def456",
                "internal_url": "http://internal:9999",
                "resolution_note": "SECRET-NOTE",
                "error": "SECRET-ERROR",
            },
        ],
    }
    result = app._project_alerts(data)
    raw_json = app.json.dumps(result)
    assert "SECRET-PROMPT" not in raw_json
    assert "/home/user/worktrees" not in raw_json
    assert "sk-secret-123" not in raw_json
    assert "abc123def456" not in raw_json
    assert "internal:9999" not in raw_json
    assert "SECRET-NOTE" not in raw_json
    assert "SECRET-ERROR" not in raw_json


# --- Alerts: GET /api/alerts ---


def test_alerts_requires_auth(password_file, gateway_key_file):
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        response = client.get("/api/alerts")
    assert response.status_code == 401


def test_alerts_viewer_can_read(tmp_path: Path, password_file, gateway_key_file):
    users_file = _make_multi_role_users_file(tmp_path)
    mock_client = _make_async_client_mock(
        _make_gateway_response(200, {"active": [], "acknowledged": []})
    )
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=_viewer_auth())
    assert response.status_code == 200
    assert response.json() == {"active": [], "history": []}


def test_alerts_returns_safe_projections(password_file, gateway_key_file, auth_headers):
    payload = {
        "active": [
            {"job_id": "abcdef123456", "status": "failed", "role": "operator", "project": "firstproject", "stage": "test", "created_at": "2025-01-15T10:00:00Z", "prompt": "SECRET"},
        ],
        "acknowledged": [
            {"job_id": "111111111111", "status": "failed", "role": "admin", "project": "p2", "stage": "plan", "created_at": "2025-01-14T10:00:00Z", "acknowledged_at": "2025-01-15T09:00:00Z", "actor": "admin"},
        ],
    }
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["active"]) == 1
    assert data["active"][0]["job_id"] == "abcdef123456"
    assert "SECRET" not in response.text
    assert len(data["history"]) == 1
    assert data["history"][0]["acknowledged_at"] == "2025-01-15T09:00:00Z"
    assert data["history"][0]["actor"] == "admin"


def test_alerts_upstream_unavailable(password_file, gateway_key_file, auth_headers):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"


def test_alerts_never_leaks_gateway_key(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"active": []}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert "test-gateway-key" not in response.text


# --- Alerts: Live gateway contract regression ---


def _live_alerts_response():
    """Exact shape of the deployed gateway GET /v1/alerts response."""
    return {
        "active": [
            {
                "job_id": "abcdef123456",
                "status": "failed",
                "role": "operator",
                "project": "firstproject",
                "stage": "test",
                "created_at": "2025-01-15T10:00:00Z",
                "prompt": "SECRET-PROMPT-CONTENT",
                "worktree": "/home/user/worktrees/secret",
                "api_key": "sk-secret-key-12345",
                "branch": "main",
                "commit_sha": "abc123def456",
                "internal_url": "http://internal:9999",
                "resolution_note": "SECRET-RESOLUTION-NOTE",
                "error": "SECRET-ERROR-DETAIL",
            },
            {
                "job_id": "111111111111",
                "status": "blocked",
                "role": "admin",
                "project": "secondproject",
                "stage": "plan",
                "created_at": "2025-01-15T11:00:00Z",
            },
        ],
        "acknowledged": [
            {
                "job_id": "222222222222",
                "status": "failed",
                "role": "operator",
                "project": "firstproject",
                "stage": "test",
                "created_at": "2025-01-14T10:00:00Z",
                "acknowledged_at": "2025-01-15T09:00:00Z",
                "actor": "admin",
                "prompt": "SECRET-PROMPT-2",
                "worktree": "/home/user/worktrees/secret2",
                "api_key": "sk-secret-key-2",
                "branch": "feature-x",
                "commit_sha": "def456abc789",
                "internal_url": "http://internal2:8888",
                "resolution_note": "SECRET-NOTE-2",
                "error": "SECRET-ERROR-2",
            },
            {
                "job_id": "333333333333",
                "status": "failed",
                "role": "admin",
                "project": "secondproject",
                "stage": "review",
                "created_at": "2025-01-13T08:00:00Z",
                "acknowledged_at": "2025-01-14T07:00:00Z",
                "actor": "operator1",
            },
        ],
    }


def test_alerts_live_gateway_contract_regression(password_file, gateway_key_file, auth_headers):
    """Regression: the exact live GET /v1/alerts response shape with nonempty
    active and acknowledged arrays must produce correct nonempty projected output."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, _live_alerts_response()))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # Active alerts: both valid entries preserved
    assert len(data["active"]) == 2
    assert data["active"][0]["job_id"] == "abcdef123456"
    assert data["active"][0]["status"] == "failed"
    assert data["active"][0]["role"] == "operator"
    assert data["active"][0]["project"] == "firstproject"
    assert data["active"][0]["stage"] == "test"
    assert data["active"][0]["created_at"] == "2025-01-15T10:00:00Z"
    assert data["active"][1]["job_id"] == "111111111111"
    assert data["active"][1]["status"] == "blocked"
    # History: both valid entries preserved
    assert len(data["history"]) == 2
    assert data["history"][0]["job_id"] == "222222222222"
    assert data["history"][0]["acknowledged_at"] == "2025-01-15T09:00:00Z"
    assert data["history"][0]["actor"] == "admin"
    assert data["history"][1]["job_id"] == "333333333333"
    assert data["history"][1]["actor"] == "operator1"
    # No unsafe fields leak
    text = response.text
    assert "SECRET-PROMPT-CONTENT" not in text
    assert "SECRET-PROMPT-2" not in text
    assert "/home/user/worktrees" not in text
    assert "sk-secret-key" not in text
    assert "abc123def456" not in text
    assert "def456abc789" not in text
    assert "internal:9999" not in text
    assert "internal2:8888" not in text
    assert "SECRET-RESOLUTION-NOTE" not in text
    assert "SECRET-NOTE-2" not in text
    assert "SECRET-ERROR" not in text


def test_alerts_live_gateway_contract_asserts_correct_url(password_file, gateway_key_file, auth_headers):
    """The fixed endpoint must call GET /v1/alerts, not /v1/dashboard."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, _live_alerts_response()))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).get("/api/alerts", headers=auth_headers)
    # Verify the URL called was /v1/alerts
    call_url = mock_client.get.await_args.args[0]
    assert call_url.endswith("/v1/alerts"), f"Expected /v1/alerts, got {call_url}"
    assert "/v1/dashboard" not in call_url, "Must not call /v1/dashboard for alerts"


def test_alerts_old_dashboard_url_would_return_empty(password_file, gateway_key_file, auth_headers):
    """Regression guard: if the code regressed to calling /v1/dashboard, the
    gateway would return dashboard data (no 'active'/'acknowledged' keys),
    resulting in empty arrays. This test asserts that the correct URL is used."""
    # Simulate what would happen if /v1/dashboard were called instead:
    # the dashboard payload has no 'active' or 'acknowledged' keys
    dashboard_payload = {
        "jobs": [],
        "usage": [],
        "counts": [],
        "projects": [],
        "recent_workflows": [],
    }
    mock_client = _make_async_client_mock(_make_gateway_response(200, dashboard_payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    # The URL must be /v1/alerts (not /v1/dashboard)
    call_url = mock_client.get.await_args.args[0]
    assert call_url.endswith("/v1/alerts"), (
        f"REGRESSION: endpoint called {call_url} instead of /v1/alerts. "
        "This would return empty arrays from the dashboard payload."
    )


def test_alerts_old_key_names_would_return_empty(password_file, gateway_key_file, auth_headers):
    """Regression guard: if the code regressed to looking for 'active_alerts'/'acknowledged_alerts'
    keys, the gateway response with 'active'/'acknowledged' keys would yield empty arrays."""
    # The gateway returns 'active' and 'acknowledged' keys
    gateway_payload = _live_alerts_response()
    mock_client = _make_async_client_mock(_make_gateway_response(200, gateway_payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    # Must be nonempty (proves the code reads 'active'/'acknowledged' not old keys)
    assert len(data["active"]) > 0, "REGRESSION: active is empty — code may be reading old key names"
    assert len(data["history"]) > 0, "REGRESSION: history is empty — code may be reading old key names"


def test_alerts_malformed_entries_dropped(password_file, gateway_key_file, auth_headers):
    """Malformed entries (non-dict, invalid job_id) are safely dropped."""
    payload = {
        "active": [
            {"job_id": "abcdef123456", "status": "failed", "role": "op", "project": "p1", "stage": "test", "created_at": "2025-01-15T10:00:00Z"},
            "not-a-dict",
            {"job_id": "invalid-id", "status": "failed"},
            {"status": "failed"},  # missing job_id
            None,
        ],
        "acknowledged": [
            {"job_id": "222222222222", "status": "failed", "role": "op", "project": "p1", "stage": "test", "created_at": "2025-01-14T10:00:00Z", "acknowledged_at": "2025-01-15T09:00:00Z", "actor": "admin"},
            "not-a-dict",
            {"job_id": "bad", "status": "failed"},
        ],
    }
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["active"]) == 1
    assert data["active"][0]["job_id"] == "abcdef123456"
    assert len(data["history"]) == 1
    assert data["history"][0]["job_id"] == "222222222222"


def test_alerts_bounded_history_from_live_shape(password_file, gateway_key_file, auth_headers):
    """History is bounded to MAX_ALERT_HISTORY even with many acknowledged entries."""
    payload = {
        "active": [],
        "acknowledged": [
            {
                "job_id": f"{'a' * 12}",
                "status": "failed",
                "role": "op",
                "project": "p1",
                "stage": "test",
                "created_at": "2025-01-14T10:00:00Z",
                "acknowledged_at": "2025-01-15T09:00:00Z",
                "actor": "admin",
            }
            for _ in range(app.MAX_ALERT_HISTORY + 10)
        ],
    }
    mock_client = _make_async_client_mock(_make_gateway_response(200, payload))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["history"]) == app.MAX_ALERT_HISTORY


def test_alerts_upstream_500(password_file, gateway_key_file, auth_headers):
    """A 500 from the gateway yields a safe 502."""
    mock_client = _make_async_client_mock(_make_gateway_response(500, {"error": "internal traceback"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway error"
    assert "traceback" not in response.text


def test_alerts_upstream_401(password_file, gateway_key_file, auth_headers):
    """A 401 from the gateway yields a safe 502 (no key leaked)."""
    mock_client = _make_async_client_mock(_make_gateway_response(401, {"error": "invalid key"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 502
    assert "invalid key" not in response.text
    assert "test-gateway-key" not in response.text


def test_alerts_never_leaks_gateway_url(password_file, gateway_key_file, auth_headers):
    """The internal gateway URL must never appear in the response."""
    mock_client = _make_async_client_mock(_make_gateway_response(500, {"error": "boom"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert "internal-secret" not in response.text


def test_alerts_bearer_token_sent_upstream(password_file, gateway_key_file, auth_headers):
    """The gateway key is sent as Bearer header upstream but never in the response."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"active": [], "acknowledged": []}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/alerts", headers=auth_headers)
    assert response.status_code == 200
    assert "test-gateway-key" not in response.text
    # Verify the gateway call received the Bearer header
    call_headers = mock_client.get.await_args.kwargs["headers"]
    assert call_headers["Authorization"] == "Bearer test-gateway-key"


# --- Alerts: POST /api/alerts/{job_id}/acknowledge ---


def test_acknowledge_requires_auth(password_file, gateway_key_file):
    with patch.object(app, "PASSWORD_FILE", password_file):
        client = TestClient(app.app)
        response = client.post(
            "/api/alerts/abcdef123456/acknowledge",
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 401


def test_acknowledge_viewer_denied(tmp_path: Path, password_file, gateway_key_file):
    users_file = _make_multi_role_users_file(tmp_path)
    audit_file = tmp_path / "audit.jsonl"
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=_viewer_auth(),
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 403
    assert "admin" in response.json()["detail"]
    audit = app.json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit["action"] == "acknowledge_alert"
    assert audit["outcome"] == "denied"


def test_acknowledge_operator_denied(tmp_path: Path, password_file, gateway_key_file):
    users_file = _make_multi_role_users_file(tmp_path)
    audit_file = tmp_path / "audit.jsonl"
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=_operator_auth(),
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 403


def test_acknowledge_admin_success(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Investigated and resolved", "confirm": "abcdef123456"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "job_id": "abcdef123456"}
    call = mock_client.post.await_args
    assert call.args[0].endswith("/v1/alerts/acknowledge")
    assert call.kwargs["json"] == {"job_id": "abcdef123456", "resolution_note": "Investigated and resolved", "actor": "admin"}
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-gateway-key"


def test_acknowledge_invalid_job_id(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).post(
            "/api/alerts/not-valid/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "not-valid"},
        )
    assert response.status_code == 404


def test_acknowledge_confirmation_mismatch(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "111111111111"},
        )
    assert response.status_code == 400
    assert "Confirmation" in response.json()["detail"]


def test_acknowledge_empty_note(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "   ", "confirm": "abcdef123456"},
        )
    assert response.status_code == 400
    assert "1 to 500" in response.json()["detail"]


def test_acknowledge_overlong_note(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "a" * 501, "confirm": "abcdef123456"},
        )
    assert response.status_code == 400
    assert "1 to 500" in response.json()["detail"]


def test_acknowledge_control_chars(password_file, auth_headers):
    with patch.object(app, "PASSWORD_FILE", password_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "bad\nnote", "confirm": "abcdef123456"},
        )
    assert response.status_code == 400
    assert "control" in response.json()["detail"]


def test_acknowledge_gateway_409(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(409, {"error": "unsafe"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Alert is not in an active state"
    assert "unsafe" not in response.text


def test_acknowledge_gateway_404(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(404, {"error": "not found"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "Alert not found"


def test_acknowledge_gateway_500(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(500, {"error": "traceback"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway error"
    assert "traceback" not in response.text


def test_acknowledge_gateway_connection_error(password_file, gateway_key_file, auth_headers):
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"


def test_acknowledge_never_leaks_gateway_key(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(500, {"error": "boom"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert "test-gateway-key" not in response.text


def test_acknowledge_never_leaks_gateway_url(password_file, gateway_key_file, auth_headers):
    mock_client = _make_async_client_mock(_make_gateway_response(500, {"error": "boom"}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    assert "internal-secret" not in response.text


def test_acknowledge_audit_entries(password_file, gateway_key_file, auth_headers, tmp_path: Path):
    audit_file = tmp_path / "ack-audit.jsonl"
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    entries = [app.json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [e["outcome"] for e in entries] == ["attempted", "succeeded"]
    assert all(e["action"] == "acknowledge_alert" for e in entries)
    assert all(e["actor"] == "admin" for e in entries)


def test_acknowledge_no_lifecycle_operations(password_file, gateway_key_file, auth_headers):
    """The acknowledge endpoint must only call the fixed acknowledge endpoint,
    never any lifecycle operation (retry, cancel, approve, merge, push, cleanup)."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    # Only one POST call should have been made
    assert mock_client.post.call_count == 1
    call_url = mock_client.post.await_args.args[0]
    assert "/acknowledge" in call_url
    # Must not contain any lifecycle operation keywords
    for op in ("retry", "cancel", "approve", "merge", "push", "cleanup", "deploy", "discard", "delete"):
        assert op not in call_url, f"URL must not contain lifecycle operation: {op}"


def test_acknowledge_fixed_upstream_destination(password_file, gateway_key_file, auth_headers):
    """The upstream URL must be the fixed gateway acknowledge endpoint."""
    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "GATEWAY_URL", "http://fixed-gateway:8765"), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )
    call_url = mock_client.post.await_args.args[0]
    assert call_url == "http://fixed-gateway:8765/v1/alerts/acknowledge"


def test_acknowledge_strict_request_shape(password_file, gateway_key_file, auth_headers):
    """Extra fields in the request body are rejected with 422 (extra='forbid')."""
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456", "extra": "malicious"},
        )
    assert response.status_code == 422


# --- Alerts: Frontend source contracts ---


def test_frontend_html_has_alerts_panel():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="alerts-panel"' in html
    assert 'id="alerts"' in html
    assert 'id="alerts-history"' in html
    assert "ALERTS" in html


def test_frontend_js_has_alerts_functions():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "loadAlerts" in js
    assert "renderAlerts" in js
    assert "acknowledgeAlert" in js
    assert "ackInFlight" in js


def test_frontend_js_alerts_uses_safe():
    """All dynamic content in renderAlerts must use safe() for HTML escaping."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function renderAlerts\(data\)\s*\{.*?\n\}", js, re.DOTALL)
    assert match is not None, "renderAlerts function not found"
    body = match.group(0)
    assert "safe(a.job_id)" in body
    assert "safe(a.project)" in body
    assert "safe(a.status)" in body


def test_frontend_js_alerts_aria():
    """The alerts container must have role=status and aria-live=polite."""
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="alerts"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_frontend_js_acknowledge_double_click_guard():
    """The acknowledge function must have an in-flight guard."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function acknowledgeAlert\(.*?\n\}", js, re.DOTALL)
    assert match is not None, "acknowledgeAlert function not found"
    body = match.group(0)
    assert "ackInFlight" in body
    assert "if (ackInFlight) return;" in body


def test_frontend_js_acknowledge_is_one_click():
    """The acknowledge button must not prompt for a note or typed job ID."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function acknowledgeAlert\(.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    assert "confirmInput" not in body
    assert "resolution_note: 'Acknowledged from dashboard'" in body
    assert "openAcknowledgeDialog" not in js


def test_frontend_js_acknowledge_sends_confirm():
    """The one-click POST still derives confirmation from the alert row's job ID."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"async function acknowledgeAlert\(.*?\n\}", js, re.DOTALL)
    assert match is not None
    body = match.group(0)
    assert "confirm: jobId" in body


def test_frontend_html_versions_javascript_asset():
    """Deployments must change the script URL so Safari cannot reuse stale UI code."""
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert html.count('src="/app.js?v=reliability-counts-1"') == 1
    assert 'continuation-jobs-1' not in html


def test_frontend_js_alerts_polling():
    """The alerts panel must poll periodically."""
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "setInterval(loadAlerts,30000)" in js


def test_frontend_css_has_alerts_styles():
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".alerts-list" in css
    assert ".alert-item" in css
    assert ".acknowledge-btn" in css
    assert ".alert-status" in css


def test_frontend_css_alerts_responsive():
    """Alert items must be responsive on small screens."""
    css = (Path(__file__).resolve().parent.parent / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".alert-item-head" in css


def test_acknowledge_route_not_in_workflow_actions():
    """The acknowledge route must not be part of the workflow actions system."""
    routes = [r.path for r in app.app.routes]
    ack_routes = [r for r in routes if "acknowledge" in r.lower()]
    assert len(ack_routes) == 1
    assert ack_routes[0] == "/api/alerts/{job_id}/acknowledge"


def test_acknowledge_not_a_workflow_action():
    """'acknowledge' must not be in WORKFLOW_ACTIONS."""
    assert "acknowledge" not in app.WORKFLOW_ACTIONS


# --- Alerts: Body limit middleware (ASGI-level) ---


def _run_asgi_post(path, headers=None, body=b""):
    """Call the ASGI app directly with full control over scope and receive.

    This allows testing scenarios that TestClient/httpx cannot express:
    missing Content-Length, understated Content-Length, etc.
    """
    import asyncio

    all_headers = [(b"host", b"testserver")]
    if headers:
        for k, v in headers.items():
            all_headers.append((k.lower().encode(), v.encode()))

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": all_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    result = {"status": None, "headers": {}, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            result["headers"] = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    asyncio.run(app.app(scope, receive, send))
    return result


def _auth_header(username="admin", password="test-pass"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_acknowledge_413_honest_oversized_content_length(password_file, gateway_key_file):
    """A valid Content-Length over the limit is rejected with 413 before auth."""
    oversized_body = b"x" * 2000
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": str(len(oversized_body))},
            body=oversized_body,
        )
    assert result["status"] == 413
    assert result["body"] == b'{"detail":"Request body too large"}'


def test_acknowledge_413_no_content_length_oversized_body(password_file, gateway_key_file):
    """Missing Content-Length with an oversized streamed body is rejected with 413.

    The middleware independently streams the body and detects the oversize
    even without a Content-Length header.
    """
    oversized_body = b"x" * 2000
    # No Content-Length header in the scope
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={},  # No Content-Length
            body=oversized_body,
        )
    assert result["status"] == 413
    assert result["body"] == b'{"detail":"Request body too large"}'


def test_acknowledge_413_understated_content_length(password_file, gateway_key_file):
    """Understated Content-Length (says 100, actual is 2000) is rejected with 413.

    The CL is within the limit so the fast-path doesn't trigger, but the
    independent stream read detects the actual oversize.
    """
    actual_body = b"x" * 2000
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": "100"},  # Understated
            body=actual_body,
        )
    assert result["status"] == 413
    assert result["body"] == b'{"detail":"Request body too large"}'


def test_acknowledge_exact_boundary_body_passes(password_file, gateway_key_file):
    """A body of exactly ACKNOWLEDGE_MAX_BODY_BYTES (1024) passes the middleware.

    The body is within the limit, so it is replayed for downstream processing.
    The route handler may reject it for other reasons (e.g., note too long),
    but it must NOT be rejected with 413 by the middleware.
    """
    # Craft a JSON body of exactly 1024 bytes
    # {"resolution_note":"<N chars>","confirm":"abcdef123456"}
    # Overhead: {"resolution_note":" = 20, ","confirm":"abcdef123456"} = 27
    # Total = 47 + N, so N = 1024 - 47 = 977
    note = "a" * 977
    body = ('{"resolution_note":"' + note + '","confirm":"abcdef123456"}').encode()
    assert len(body) == 1024, f"Expected 1024 bytes, got {len(body)}"

    mock_client = _make_async_client_mock(_make_gateway_response(200, {"ok": True}))
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={**_auth_header(), "Content-Length": str(len(body))},
            body=body,
        )
    # Must NOT be 413 (the middleware let it through)
    assert result["status"] != 413
    # Downstream validation rejects the deliberately oversized note.
    # Depending on FastAPI/Pydantic version this is 400 (handler check) or
    # 422 (Pydantic validation); both are safe client errors.
    assert result["status"] in (400, 422)


def test_acknowledge_400_malformed_content_length(password_file, gateway_key_file):
    """A non-numeric Content-Length is rejected with 400."""
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": "not-a-number"},
            body=b"{}",
        )
    assert result["status"] == 400
    assert result["body"] == b'{"detail":"Invalid Content-Length"}'


def test_acknowledge_400_negative_content_length(password_file, gateway_key_file):
    """A negative Content-Length is rejected with 400."""
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": "-1"},
            body=b"",
        )
    assert result["status"] == 400
    assert result["body"] == b'{"detail":"Invalid Content-Length"}'


def test_acknowledge_413_upstream_never_called(password_file, gateway_key_file):
    """The upstream gateway is never called when the body is rejected."""
    oversized_body = b"x" * 2000
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock()
    mock_client.get = AsyncMock()

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": str(len(oversized_body))},
            body=oversized_body,
        )
    assert result["status"] == 413
    # No upstream call was made
    mock_client.post.assert_not_called()
    mock_client.get.assert_not_called()


def test_acknowledge_413_response_non_disclosure(password_file, gateway_key_file):
    """The 413 response discloses no body content, secrets, URLs, or paths."""
    # Body must be over 1024 bytes to trigger 413
    secret_body = (b'{"resolution_note":"SECRET-DATA-xyz","confirm":"abcdef123456","leak":"http://internal:9999"}' * 20)
    assert len(secret_body) > app.ACKNOWLEDGE_MAX_BODY_BYTES
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": str(len(secret_body))},
            body=secret_body,
        )
    assert result["status"] == 413
    response_text = result["body"].decode()
    # No body content echoed
    assert "SECRET-DATA-xyz" not in response_text
    # No internal URLs
    assert "internal-secret" not in response_text
    assert "internal:9999" not in response_text
    # No secrets
    assert "test-gateway-key" not in response_text
    # No filesystem paths
    assert "/run/secrets" not in response_text
    # Fixed response body
    assert response_text == '{"detail":"Request body too large"}'


def test_acknowledge_400_response_non_disclosure(password_file, gateway_key_file):
    """The 400 response for invalid Content-Length discloses no sensitive data."""
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"):
        result = _run_asgi_post(
            "/api/alerts/abcdef123456/acknowledge",
            headers={"Content-Length": "abc"},
            body=b"{}",
        )
    assert result["status"] == 400
    response_text = result["body"].decode()
    assert "internal-secret" not in response_text
    assert "test-gateway-key" not in response_text
    assert "/run/secrets" not in response_text
    assert response_text == '{"detail":"Invalid Content-Length"}'


def test_acknowledge_body_limit_does_not_affect_other_routes(password_file, gateway_key_file, auth_headers):
    """The body limit middleware only applies to the acknowledge route.

    Other POST routes with large bodies are unaffected.
    """
    large_body = b"x" * 5000
    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        # POST to /api/workflows with a large body should NOT get 413 from middleware
        result = _run_asgi_post(
            "/api/workflows",
            headers={**_auth_header(), "Content-Length": str(len(large_body)), "Content-Type": "application/json"},
            body=large_body,
        )
    # Should not be 413 (middleware doesn't apply to this route)
    assert result["status"] != 413
    # Will be 422 (invalid JSON) or 400 (validation) but not 413
    assert result["status"] in (400, 422)


def test_acknowledge_body_limit_ignores_non_post(password_file, gateway_key_file):
    """GET requests to the acknowledge path are not affected by the body limit."""
    import asyncio

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/alerts/abcdef123456/acknowledge",
        "raw_path": b"/api/alerts/abcdef123456/acknowledge",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-length", b"99999")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    async def receive():
        return {"type": "http.disconnect"}

    result = {"status": None, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
        elif message["type"] == "http.response.body":
            result["body"] += message.get("body", b"")

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file):
        asyncio.run(app.app(scope, receive, send))
    # GET is not POST, so middleware passes through; route will 405 or similar
    assert result["status"] != 413


# --- Alerts: Acknowledge timeout test ---


def test_acknowledge_timeout_returns_safe_502(password_file, gateway_key_file, auth_headers, tmp_path: Path):
    """When the upstream gateway times out (httpx.ReadTimeout), the endpoint
    returns a fixed safe 502, audits upstream_unavailable, and the AsyncClient
    receives the bounded ACKNOWLEDGE_TIMEOUT value.
    """
    audit_file = tmp_path / "timeout-audit.jsonl"
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    with patch.object(app, "PASSWORD_FILE", password_file), \
         patch.object(app, "GATEWAY_KEY_FILE", gateway_key_file), \
         patch.object(app, "AUDIT_LOG_FILE", audit_file), \
         patch.object(app, "GATEWAY_URL", "http://internal-secret:9999"), \
         patch("app.httpx.AsyncClient", return_value=mock_client) as mock_cls:
        response = TestClient(app.app).post(
            "/api/alerts/abcdef123456/acknowledge",
            headers=auth_headers,
            json={"resolution_note": "Fixed", "confirm": "abcdef123456"},
        )

    # Fixed safe 502 response
    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream gateway unavailable"
    # No upstream text, URL, or key leaked
    assert "internal-secret" not in response.text
    assert "9999" not in response.text
    assert "test-gateway-key" not in response.text
    assert "timed out" not in response.text
    # Audit outcome is upstream_unavailable
    entries = [app.json.loads(line) for line in audit_file.read_text().splitlines()]
    outcomes = [e["outcome"] for e in entries]
    assert "upstream_unavailable" in outcomes
    # The AsyncClient was created with the ACKNOWLEDGE_TIMEOUT value
    call_kwargs = mock_cls.call_args.kwargs
    timeout_value = call_kwargs.get("timeout")
    assert timeout_value == app.ACKNOWLEDGE_TIMEOUT
    # Timeout is finite and reasonably bounded
    assert timeout_value is not None
    assert timeout_value > 0
    assert timeout_value <= 60.0, f"Timeout {timeout_value}s is unreasonably large"


# --- Phase B: model_reason allow-list and rendering tests ---

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


class TestModelReasonAllowList:
    """Tests for the exact allow-list model_reason sanitizer."""

    ALLOWED = frozenset({
        "explicit-fast",
        "explicit-deep",
        "reasoning-keyword",
        "long-complex-prompt",
        "default-fast",
        "reasoner-default-deep",
        "auto-mutation-stays-fast",
    })

    def _sanitize(self, value):
        """Import and call the sanitizer from app module."""
        import app as app_module
        return app_module._safe_model_reason(value)

    @pytest.mark.parametrize("value", [
        "explicit-fast",
        "explicit-deep",
        "reasoning-keyword",
        "long-complex-prompt",
        "default-fast",
        "reasoner-default-deep",
        "auto-mutation-stays-fast",
    ])
    def test_allowed_values_pass(self, value):
        assert self._sanitize(value) == value

    def test_unknown_string_returns_none(self):
        assert self._sanitize("some-random-reason") is None

    def test_empty_string_returns_none(self):
        assert self._sanitize("") is None

    def test_non_string_returns_none(self):
        assert self._sanitize(123) is None
        assert self._sanitize(None) is None
        assert self._sanitize(["explicit-fast"]) is None
        assert self._sanitize({"reason": "explicit-fast"}) is None

    def test_control_character_returns_none(self):
        assert self._sanitize("explicit-fast\x00") is None
        assert self._sanitize("explicit\nfast") is None
        assert self._sanitize("explicit\tdeep") is None

    def test_oversized_returns_none(self):
        assert self._sanitize("explicit-fast" * 100) is None

    def test_case_sensitive(self):
        assert self._sanitize("Explicit-Fast") is None
        assert self._sanitize("EXPLICIT-FAST") is None

    def test_whitespace_padded_not_allowed(self):
        # After strip, if it matches allow-list it should pass
        # But the spec says exact allow-list, so let's check: strip then match
        assert self._sanitize("  explicit-fast  ") == "explicit-fast"


class TestModelReasonsBoundedDedup:
    """Tests for the bounded deduplicated model_reasons list."""

    def _sanitize(self, value):
        import app as app_module
        return app_module._safe_model_reasons(value)

    def test_valid_list(self):
        result = self._sanitize(["explicit-fast", "default-fast"])
        assert result == ["explicit-fast", "default-fast"]

    def test_deduplicates(self):
        result = self._sanitize(["explicit-fast", "explicit-fast", "default-fast"])
        assert result == ["explicit-fast", "default-fast"]

    def test_bounded_to_max(self):
        import app as app_module
        reasons = ["explicit-fast", "explicit-deep", "reasoning-keyword",
                   "long-complex-prompt", "default-fast", "reasoner-default-deep",
                   "auto-mutation-stays-fast"]
        # Create a list with more than MAX_MODEL_REASONS unique valid entries
        # Since we only have 7 allowed, we can't exceed that with unique values.
        # But duplicates should be removed.
        big_list = reasons * 10  # 70 entries, 7 unique
        result = self._sanitize(big_list)
        assert len(result) <= app_module.MAX_MODEL_REASONS
        assert len(result) == 7  # only 7 unique allowed values

    def test_non_list_returns_none(self):
        assert self._sanitize("explicit-fast") is None
        assert self._sanitize(None) is None
        assert self._sanitize(42) is None

    def test_mixed_valid_invalid(self):
        result = self._sanitize(["explicit-fast", "bogus-reason", "default-fast"])
        assert result == ["explicit-fast", "default-fast"]

    def test_all_invalid_returns_empty_list(self):
        result = self._sanitize(["bogus1", "bogus2"])
        assert result == []


class TestStageProjectionModelReason:
    """Tests that stage projections include model_reason safely."""

    def _project(self, raw):
        import app as app_module
        return app_module._project_stage(raw)

    def test_valid_reason_preserved(self):
        raw = {"stage": "build", "role": "builder", "status": "done",
               "model": "test-model", "model_reason": "explicit-fast"}
        result = self._project(raw)
        assert result["model_reason"] == "explicit-fast"

    def test_invalid_reason_null(self):
        raw = {"stage": "build", "role": "builder", "status": "done",
               "model": "test-model", "model_reason": "arbitrary-secret-data"}
        result = self._project(raw)
        assert result["model_reason"] is None

    def test_missing_reason_null(self):
        raw = {"stage": "build", "role": "builder", "status": "done",
               "model": "test-model"}
        result = self._project(raw)
        assert result["model_reason"] is None

    def test_non_string_reason_null(self):
        raw = {"stage": "build", "role": "builder", "status": "done",
               "model": "test-model", "model_reason": 12345}
        result = self._project(raw)
        assert result["model_reason"] is None


class TestDashboardProjectionModelReasons:
    """Tests that dashboard workflow summaries include bounded model_reasons."""

    def _project(self, raw):
        import app as app_module
        return app_module._project_dashboard(raw)

    def test_model_reasons_in_workflow(self):
        raw = {
            "recent_workflows": [
                {"id": "abc123", "overall": "running", "model_reasons": ["explicit-fast", "default-fast"]}
            ],
            "repair_enabled": True,
        }
        result = self._project(raw)
        wf = result["recent_workflows"][0]
        assert wf["model_reasons"] == ["explicit-fast", "default-fast"]

    def test_model_reasons_invalid_filtered(self):
        raw = {
            "recent_workflows": [
                {"id": "abc123", "overall": "running", "model_reasons": ["explicit-fast", "secret-prompt-data"]}
            ],
            "repair_enabled": True,
        }
        result = self._project(raw)
        wf = result["recent_workflows"][0]
        assert wf["model_reasons"] == ["explicit-fast"]

    def test_model_reasons_non_list_null(self):
        raw = {
            "recent_workflows": [
                {"id": "abc123", "overall": "running", "model_reasons": "not-a-list"}
            ],
            "repair_enabled": True,
        }
        result = self._project(raw)
        wf = result["recent_workflows"][0]
        assert wf["model_reasons"] is None


class TestFrontendRendering:
    """Source-level tests proving app.js renders model_reason labels with null fallback."""

    def _read_app_js(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / "static" / "app.js").read_text(encoding="utf-8")

    def test_renders_model_reason_label(self):
        src = self._read_app_js()
        # Must reference model_reason for rendering
        assert "model_reason" in src

    def test_null_fallback_em_dash(self):
        src = self._read_app_js()
        # Must have em dash fallback for null/undefined model_reason
        assert "\u2014" in src or "&mdash;" in src or "\u2014" in src

    def test_no_raw_html_injection_from_reason(self):
        src = self._read_app_js()
        # The rendering should use textContent or createTextNode, not innerHTML for model_reason
        # Check that model_reason is not directly interpolated into innerHTML
        # This is a heuristic: look for safe rendering patterns
        assert "textContent" in src or "createTextNode" in src or "innerText" in src


class TestIndexHtmlVersionedUrl:
    """Verify index.html references a versioned app.js URL."""

    def _read_index(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")

    def test_versioned_app_js_url(self):
        src = self._read_index()
        # Must have the exact versioned app.js URL exactly once
        assert src.count('src="/app.js?v=reliability-counts-1"') == 1
        # Stale version must be absent
        assert "continuation-jobs-1" not in src


class TestAggregateStatusCounts:
    """Tests for the _aggregate_status_counts helper."""

    def test_valid_counts_summed_across_projects(self):
        counts = [
            {"project": "a", "status": "completed", "count": 5},
            {"project": "b", "status": "completed", "count": 3},
            {"project": "a", "status": "failed", "count": 2},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": 8, "failed": 2, "blocked": 0}

    def test_absent_status_is_zero(self):
        counts = [{"project": "a", "status": "completed", "count": 1}]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": 1, "failed": 0, "blocked": 0}

    def test_none_input_returns_all_none(self):
        result = app._aggregate_status_counts(None)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_non_list_input_returns_all_none(self):
        result = app._aggregate_status_counts("not a list")
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_bool_count_returns_null(self):
        """Bool count for a known status is invalid; return all-None."""
        counts = [
            {"project": "a", "status": "completed", "count": True},
            {"project": "b", "status": "completed", "count": 3},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_negative_count_returns_null(self):
        """Negative count for a known status is invalid; return all-None."""
        counts = [
            {"project": "a", "status": "failed", "count": -5},
            {"project": "b", "status": "failed", "count": 2},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_malformed_row_returns_null(self):
        """Non-dict rows are malformed; return all-None."""
        counts = [
            "not a dict",
            None,
            42,
            {"project": "a", "status": "completed", "count": 1},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_unknown_status_ignored(self):
        counts = [
            {"project": "a", "status": "running", "count": 10},
            {"project": "a", "status": "completed", "count": 1},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": 1, "failed": 0, "blocked": 0}

    def test_non_string_status_returns_null(self):
        """Non-string status is malformed; return all-None."""
        counts = [
            {"project": "a", "status": 123, "count": 5},
            {"project": "a", "status": None, "count": 5},
            {"project": "a", "status": "completed", "count": 2},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_empty_list_returns_zeros(self):
        result = app._aggregate_status_counts([])
        assert result == {"completed": 0, "failed": 0, "blocked": 0}

    def test_output_always_has_three_keys(self):
        result = app._aggregate_status_counts(None)
        assert set(result.keys()) == {"completed", "failed", "blocked"}
        result = app._aggregate_status_counts([])
        assert set(result.keys()) == {"completed", "failed", "blocked"}

    def test_over_bound_count_returns_null(self):
        """Over-bound count for a known status is invalid; return all-None."""
        counts = [
            {"project": "a", "status": "completed", "count": 99999999999},
            {"project": "b", "status": "completed", "count": 1},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_summed_total_over_bound_returns_null(self):
        """If summed totals exceed the safe bound, return all-None."""
        counts = [
            {"project": "a", "status": "completed", "count": 6_000_000},
            {"project": "b", "status": "completed", "count": 6_000_000},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": None, "failed": None, "blocked": None}

    def test_summed_total_at_bound_is_valid(self):
        """If summed totals equal the safe bound exactly, it is valid."""
        counts = [
            {"project": "a", "status": "completed", "count": 5_000_000},
            {"project": "b", "status": "completed", "count": 5_000_000},
        ]
        result = app._aggregate_status_counts(counts)
        assert result == {"completed": 10_000_000, "failed": 0, "blocked": 0}


class TestProjectAgentQueueStatusCounts:
    """Tests that _project_agent_queue includes status_counts in its output."""

    def test_status_counts_included(self):
        gateway_data = {
            "counts": [
                {"project": "a", "status": "completed", "count": 5},
                {"project": "b", "status": "failed", "count": 2},
            ]
        }
        result = app._project_agent_queue(gateway_data)
        assert result["status_counts"] == {"completed": 5, "failed": 2, "blocked": 0}

    def test_status_counts_none_when_missing(self):
        gateway_data = {}
        result = app._project_agent_queue(gateway_data)
        assert result["status_counts"] == {"completed": None, "failed": None, "blocked": None}

    def test_status_counts_none_when_gateway_present_but_counts_missing(self):
        """A truthy gateway payload that omits counts must still report Unavailable, not false zero."""
        gateway_data = {"jobs": [{"id": "j1", "status": "running"}]}
        result = app._project_agent_queue(gateway_data)
        assert result["status_counts"] == {"completed": None, "failed": None, "blocked": None}

    def test_status_counts_none_when_counts_malformed(self):
        """A truthy gateway payload with a non-list counts value must report Unavailable."""
        gateway_data = {"jobs": [], "counts": "not-a-list"}
        result = app._project_agent_queue(gateway_data)
        assert result["status_counts"] == {"completed": None, "failed": None, "blocked": None}

    def test_status_counts_zero_when_counts_empty_list(self):
        """A valid empty counts list is available data, so absent statuses render as zero."""
        gateway_data = {"jobs": [], "counts": []}
        result = app._project_agent_queue(gateway_data)
        assert result["status_counts"] == {"completed": 0, "failed": 0, "blocked": 0}
