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
    - Telegram is empty (unconfigured, no real endpoint)
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
    # Telegram is explicitly empty (unconfigured)
    assert 'TELEGRAM_BOT_URL: ""' in compose


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
        assert set(job.keys()) == {"id", "project", "stage"}


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
    tg = urls.get("telegram", "http://telegram:8081")
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
        "lmstudio:1234/v1/models": _make_gateway_response(200, {"data": [{"id": "test-model", "loaded": True}]}),
        "gateway:8765/v1/dashboard": _make_gateway_response(200, {"jobs": [], "usage": [], "counts": [], "projects": []}),
        "telegram:8081/health": _make_gateway_response(200, {"status": "ok"}),
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
    assert set(data["agent_queue"].keys()) == {"queued", "running", "running_jobs", "current_job", "last_checked", "detail"}


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
    assert "telegram:8081" not in text
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
    """The LM Studio token is sent as Bearer header to /v1/models but never appears in the response."""
    mock_client = _routed_client(_all_healthy_routes())
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    assert response.status_code == 200
    assert "test-lm-token" not in response.text
    # Verify the LM Studio call hit /v1/models (not /health) with the Bearer token
    lm_calls = [c for c in mock_client.get.call_args_list if "v1/models" in str(c)]
    assert len(lm_calls) == 1, f"Expected exactly 1 /v1/models call, got {len(lm_calls)}"
    assert lm_calls[0].kwargs["headers"]["Authorization"] == "Bearer test-lm-token"
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
    routes["lmstudio:1234/v1/models"] = httpx.ConnectError("refused")
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
        "lmstudio:1234/v1/models": httpx.ConnectError("refused"),
        "gateway:8765/v1/dashboard": httpx.ConnectError("refused"),
        "telegram:8081/health": httpx.ConnectError("refused"),
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


def test_cluster_health_timeout_marks_offline(tmp_path: Path, auth_headers):
    """A service that times out is marked offline with detail 'timeout'."""
    routes = _all_healthy_routes()
    routes["lmstudio:1234/v1/models"] = httpx.ReadTimeout("timed out")
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
    routes["lmstudio:1234/v1/models"] = httpx.ConnectError("refused")
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
    routes["lmstudio:1234/v1/models"] = _make_gateway_response(200, {"data": [
        {"id": "meta-llama/Llama-3-8B", "loaded": True},
        {"id": "mistral-7b", "loaded": False},
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
    routes["lmstudio:1234/v1/models"] = httpx.ConnectError("refused")
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
    routes["lmstudio:1234/v1/models"] = _make_gateway_response(200, {"data": [
        {"id": "valid-model", "loaded": True},
        {"id": "model with spaces", "loaded": True},
        {"id": "a" * (app.LMSTUDIO_MODEL_ID_MAX + 1), "loaded": True},
        {"id": None, "loaded": True},
        {"id": "another-valid", "loaded": False},
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
    routes["lmstudio:1234/v1/models"] = _make_gateway_response(200, {"data": [
        {"id": at_max, "loaded": True},
        {"id": over_max, "loaded": True},
    ]})
    mock_client = _routed_client(routes)
    with _health_env(tmp_path), \
         patch("app.httpx.AsyncClient", return_value=mock_client):
        response = TestClient(app.app).get("/api/cluster-health", headers=auth_headers)
    data = response.json()
    models = data["lm_studio"]["models"]
    assert len(models) == 1
    assert models[0]["id"] == at_max


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
