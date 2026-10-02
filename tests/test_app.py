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
    assert "w.overall" in js
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
    assert 'id="build-objective"' in html
    assert 'id="build-submit"' in html
    assert 'id="build-status"' in html
    assert 'id="build-char-count"' in html


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
    # A changed intent generates a fresh key.
    assert "buildKey = crypto.randomUUID();" in js


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
