"""Focused API tests for the admin-only account-management endpoints.

Covers: viewer/operator denial, admin success, CSRF, sanitized output,
validation, duplicate/unknown users, session revocation, last-admin
protection, malformed-file preservation, and atomic persistence.
"""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module
from app import (
    app,
    create_password_record,
    _sessions,
    _session_lock,
    _users_lock,
    _login_attempts,
    _login_rate_lock,
)
from fastapi.testclient import TestClient

ADMIN_PW = "correct-horse-battery-staple-123"


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    """Redirect persistent files to a temp dir and reset in-memory state."""
    users_file = tmp_path / "users.json"
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(app_module, "USERS_FILE", users_file)
    monkeypatch.setattr(app_module, "AUDIT_LOG_FILE", audit_file)
    with _session_lock:
        _sessions.clear()
    with _login_rate_lock:
        _login_attempts.clear()
    return {
        "users_file": users_file,
        "audit_file": audit_file,
    }


def _seed_user(username, role="admin", disabled=False):
    """Seed a user record directly into the users file."""
    record = create_password_record(ADMIN_PW, role)
    record["disabled"] = disabled
    with _users_lock:
        users = app_module.load_users_unlocked()
        users[username] = record
        app_module.save_users_atomic(users)


def _login(client, username, password=ADMIN_PW):
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp


def _csrf(client, username, password=ADMIN_PW):
    """Log in and return the CSRF token for the session."""
    _login(client, username, password)
    resp = client.get("/api/session")
    assert resp.status_code == 200
    return resp.json()["csrf_token"]


def _basic(username, password=ADMIN_PW):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _read_audit():
    audit_file = app_module.AUDIT_LOG_FILE
    if not audit_file.is_file():
        return []
    return [json.loads(line) for line in audit_file.read_text().splitlines() if line.strip()]


def _audit_actions():
    return [(e["action"], e["outcome"]) for e in _read_audit()]


def _target_session_count(username):
    with _session_lock:
        return sum(1 for v in _sessions.values() if v["username"] == username)


class TestAuthorization:
    def test_viewer_denied_list(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("viewer1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "viewer1")
        resp = client.get("/api/admin/users", headers={"X-CSRF-Token": token})
        assert resp.status_code == 403

    def test_operator_denied_create(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("operator1", "operator")
        client = TestClient(app)
        token = _csrf(client, "operator1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 403

    def test_admin_allowed_list(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.get("/api/admin/users", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200

    def test_unauthenticated_denied(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401


class TestListSanitized:
    def test_projection_only_username_role_disabled(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("viewer1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.get("/api/admin/users", headers={"X-CSRF-Token": token})
        assert resp.status_code == 200
        users = resp.json()["users"]
        for u in users:
            assert set(u.keys()) == {"username", "role", "disabled"}
        body = resp.text
        assert "password_hash" not in body
        assert "salt" not in body


class TestCreateUser:
    def test_create_success(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["user"]["username"] == "newuser"
        assert resp.json()["user"]["role"] == "viewer"

    def test_create_duplicate_409(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "admin1", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 409

    def test_create_invalid_username_400(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "UPPER", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 400

    def test_create_short_password_400(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "short", "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 400

    def test_create_blank_password_400(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "   ", "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 400

    def test_create_invalid_role_400(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "superuser"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 400


class TestChangeRole:
    def test_change_role_success_and_revoke(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("target", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        # Create a session for target via a SEPARATE client so the admin
        # client's session cookie (and role) is not overwritten.
        target_client = TestClient(app)
        _login(target_client, "target")
        assert _target_session_count("target") >= 1
        resp = client.patch(
            "/api/admin/users/target",
            json={"role": "operator"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "operator"
        assert _target_session_count("target") == 0

    def test_demote_last_admin_409(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.patch(
            "/api/admin/users/admin1",
            json={"role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 409

    def test_unknown_user_404(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.patch(
            "/api/admin/users/ghost",
            json={"role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 404


class TestSetDisabled:
    def test_disable_success_and_revoke(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("target", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        target_client = TestClient(app)
        _login(target_client, "target")
        assert _target_session_count("target") >= 1
        resp = client.patch(
            "/api/admin/users/target/disabled",
            json={"disabled": True},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["disabled"] is True
        assert _target_session_count("target") == 0

    def test_disable_last_admin_409(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.patch(
            "/api/admin/users/admin1/disabled",
            json={"disabled": True},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 409

    def test_enable_success(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("target", "viewer", disabled=True)
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.patch(
            "/api/admin/users/target/disabled",
            json={"disabled": False},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["disabled"] is False


class TestResetPassword:
    def test_reset_success_and_revoke(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("target", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        target_client = TestClient(app)
        _login(target_client, "target")
        assert _target_session_count("target") >= 1
        resp = client.post(
            "/api/admin/users/target/reset-password",
            json={"password": "new" + "x" * 15},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert _target_session_count("target") == 0

    def test_reset_unknown_404(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users/ghost/reset-password",
            json={"password": "new" + "x" * 15},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 404

    def test_reset_short_password_400(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("target", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users/target/reset-password",
            json={"password": "short"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 400


class TestCSRF:
    def test_missing_csrf_403(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        _login(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
        )
        assert resp.status_code == 403

    def test_wrong_csrf_403(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        _login(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": "wrong-token"},
        )
        assert resp.status_code == 403

    def test_basic_auth_bypasses_csrf(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        resp = client.get("/api/admin/users", headers=_basic("admin1"))
        assert resp.status_code == 200


class TestAudit:
    def test_denied_audited(self, tmp_env):
        _seed_user("admin1", "admin")
        _seed_user("viewer1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "viewer1")
        client.get("/api/admin/users", headers={"X-CSRF-Token": token})
        assert ("list_users", "denied") in _audit_actions()

    def test_success_audited(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert ("create_user", "succeeded") in _audit_actions()

    def test_failed_audited_on_malformed(self, tmp_env):
        users_file = tmp_env["users_file"]
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        users_file.write_text("{corrupted")
        client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert ("create_user", "failed") in _audit_actions()

    def test_audit_no_secrets(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        audit_text = app_module.AUDIT_LOG_FILE.read_text()
        assert "a" * 16 not in audit_text
        assert "password_hash" not in audit_text
        assert "salt" not in audit_text


class TestMalformedFile:
    def test_malformed_file_preserved_on_create(self, tmp_env):
        """A malformed users file must not be overwritten by a mutation."""
        users_file = tmp_env["users_file"]
        _seed_user("admin1", "admin")  # valid file
        client = TestClient(app)
        token = _csrf(client, "admin1")  # authenticate while file is valid
        users_file.write_text("{corrupted")  # corrupt after authentication
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        assert users_file.read_text() == "{corrupted"

    def test_missing_file_initializes(self, tmp_env):
        """A missing users file may be initialized empty on first write."""
        users_file = tmp_env["users_file"]
        assert not users_file.is_file()
        record = create_password_record(ADMIN_PW, "admin")
        record["disabled"] = False
        users_file.write_text(json.dumps({"admin1": record}))
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        data = json.loads(users_file.read_text())
        assert "admin1" in data
        assert "newuser" in data


class TestAtomicPersistence:
    def test_file_is_valid_json_after_mutation(self, tmp_env):
        users_file = tmp_env["users_file"]
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "a" * 16, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        # The persisted file must always be valid JSON (atomic write).
        data = json.loads(users_file.read_text())
        assert "admin1" in data
        assert "newuser" in data
        # No partial/temp files left behind.
        leftovers = [p.name for p in users_file.parent.iterdir() if p.name.startswith("users.json")]
        assert leftovers == ["users.json"]


class TestNoDeletion:
    def test_no_delete_endpoint(self, tmp_env):
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.delete("/api/admin/users/target", headers={"X-CSRF-Token": token})
        assert resp.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Regression: validation errors must not echo secrets (Fix 1)
# ---------------------------------------------------------------------------

class TestValidationSanitization:
    """422 responses must not contain input values or context that may
    include password-bearing secrets."""

    SECRET = "S3cr3t-Marker-UNIQUE-9f8a7b6c"

    def test_create_user_invalid_password_type_no_secret_in_422(self, tmp_env):
        """Password field with wrong type (list) containing a secret marker
        must not appear in the 422 response body."""
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": [self.SECRET], "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 422
        assert self.SECRET not in resp.text, f"Secret leaked in 422: {resp.text}"

    def test_create_user_extra_field_with_secret_no_leak(self, tmp_env):
        """Extra field containing a secret must not appear in response."""
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "validpass12345678", "role": "viewer",
                  "password_hint": self.SECRET},
            headers={"X-CSRF-Token": token},
        )
        # Whether 422 (extra=forbid) or 201 (extra=ignore), no secret leak
        assert self.SECRET not in resp.text

    def test_reset_password_invalid_type_no_secret_in_422(self, tmp_env):
        """Reset-password with wrong-type password must not echo the secret."""
        _seed_user("admin1", "admin")
        _seed_user("target1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users/target1/reset-password",
            json={"password": {"nested": self.SECRET}},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 422
        assert self.SECRET not in resp.text

    def test_login_invalid_password_type_no_secret_in_422(self, tmp_env):
        """Login with wrong-type password must not echo the secret."""
        client = TestClient(app)
        resp = client.post(
            "/api/login",
            json={"username": "someone", "password": [self.SECRET]},
        )
        assert resp.status_code == 422
        assert self.SECRET not in resp.text

    def test_validation_422_preserves_field_and_type_info(self, tmp_env):
        """Sanitized 422 should still include field name and error type for
        useful debugging, just not the input value."""
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": 12345, "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 422
        data = resp.json()
        assert "detail" in data
        errors = data["detail"]
        assert len(errors) > 0
        # Field name should be present
        assert any("password" in str(e.get("loc", [])) for e in errors)
        # Message should be present
        assert any(e.get("msg") for e in errors)
        # Input values must be stripped
        for e in errors:
            assert "input" not in e or e["input"] is None, f"Input value leaked: {e}"
            assert "ctx" not in e or e["ctx"] is None, f"Ctx leaked: {e}"


# ---------------------------------------------------------------------------
# Regression: persistence failure must produce controlled 500, failed audit,
# no session revocation, no success audit, file preserved (Fix 2)
# ---------------------------------------------------------------------------

class TestSaveFailureHandling:
    """When save_users_atomic raises OSError, all admin mutation routes must:
    - Return 500 with a controlled (secret-free) message
    - Audit "failed"
    - NOT audit "succeeded"
    - NOT revoke sessions
    - Preserve the on-disk users file unchanged
    """

    def test_create_user_save_failure(self, tmp_env, monkeypatch):
        import app as app_module
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        original_content = app_module.USERS_FILE.read_text()

        def _failing_save(users):
            raise OSError("disk full")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "newpass12345678", "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        # Controlled message, no internal details
        assert "disk full" not in resp.text
        assert "OSError" not in resp.text
        # File preserved
        assert app_module.USERS_FILE.read_text() == original_content
        # Audit: failed present, succeeded absent
        audit = _read_audit()
        statuses = [e["outcome"] for e in audit if e["action"] == "create_user" and e["workflow_id"] == "newuser"]
        assert "failed" in statuses
        assert "succeeded" not in statuses

    def test_change_role_save_failure(self, tmp_env, monkeypatch):
        import app as app_module
        _seed_user("admin1", "admin")
        _seed_user("target1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        original_content = app_module.USERS_FILE.read_text()

        # Create a session for target1 so we can verify no revocation
        session_id = app_module._create_session("target1", "viewer")

        def _failing_save(users):
            raise OSError("permission denied")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        resp = client.patch(
            "/api/admin/users/target1",
            json={"role": "operator"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        assert "permission denied" not in resp.text
        # File preserved
        assert app_module.USERS_FILE.read_text() == original_content
        # Session NOT revoked
        assert app_module._validate_session(session_id) is not None
        # Audit: failed, not succeeded
        audit = _read_audit()
        statuses = [e["outcome"] for e in audit if e["action"] == "change_role" and e["workflow_id"] == "target1"]
        assert "failed" in statuses
        assert "succeeded" not in statuses

    def test_set_disabled_save_failure(self, tmp_env, monkeypatch):
        import app as app_module
        _seed_user("admin1", "admin")
        _seed_user("target1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        original_content = app_module.USERS_FILE.read_text()

        session_id = app_module._create_session("target1", "viewer")

        def _failing_save(users):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        resp = client.patch(
            "/api/admin/users/target1/disabled",
            json={"disabled": True},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        assert "read-only" not in resp.text
        assert app_module.USERS_FILE.read_text() == original_content
        # Session NOT revoked
        assert app_module._validate_session(session_id) is not None
        # Audit: failed, not succeeded
        audit = _read_audit()
        statuses = [e["outcome"] for e in audit if e["action"] == "set_disabled" and e["workflow_id"] == "target1"]
        assert "failed" in statuses
        assert "succeeded" not in statuses

    def test_reset_password_save_failure(self, tmp_env, monkeypatch):
        import app as app_module
        _seed_user("admin1", "admin")
        _seed_user("target1", "viewer")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        original_content = app_module.USERS_FILE.read_text()

        session_id = app_module._create_session("target1", "viewer")

        def _failing_save(users):
            raise OSError("I/O error")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        resp = client.post(
            "/api/admin/users/target1/reset-password",
            json={"password": "newpass45678901"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        assert "I/O error" not in resp.text
        assert app_module.USERS_FILE.read_text() == original_content
        # Session NOT revoked
        assert app_module._validate_session(session_id) is not None
        # Audit: failed, not succeeded
        audit = _read_audit()
        statuses = [e["outcome"] for e in audit if e["action"] == "reset_password" and e["workflow_id"] == "target1"]
        assert "failed" in statuses
        assert "succeeded" not in statuses

    def test_save_failure_serialization_error(self, tmp_env, monkeypatch):
        """A TypeError from json.dump (non-serializable data) is also caught."""
        import app as app_module
        _seed_user("admin1", "admin")
        client = TestClient(app)
        token = _csrf(client, "admin1")
        original_content = app_module.USERS_FILE.read_text()

        def _failing_save(users):
            raise TypeError("Object of type set is not JSON serializable")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        resp = client.post(
            "/api/admin/users",
            json={"username": "newuser", "password": "newpass12345678", "role": "viewer"},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 500
        assert "serializable" not in resp.text
        assert app_module.USERS_FILE.read_text() == original_content


# ---------------------------------------------------------------------------
# Regression: mutate_users strict mode and PersistenceError (Fix 3)
# ---------------------------------------------------------------------------

class TestMutateUsersStrict:
    """Verify the consolidated mutate_users helper with strict=True."""

    def test_strict_directory_is_not_missing(self, tmp_env):
        app_module.USERS_FILE.mkdir()
        with pytest.raises(ValueError):
            app_module.mutate_users(lambda users: users.clear(), strict=True)
        assert app_module.USERS_FILE.is_dir()

    def test_strict_permission_failure_does_not_save(self, tmp_env, monkeypatch):
        app_module.USERS_FILE.write_text('{"admin1": {"role": "admin"}}')
        original = app_module.USERS_FILE.read_bytes()
        path_type = type(app_module.USERS_FILE)
        real_read = path_type.read_text
        with monkeypatch.context() as patcher:
            def denied(path, *args, **kwargs):
                if path == app_module.USERS_FILE:
                    raise PermissionError("test permission failure")
                return real_read(path, *args, **kwargs)
            patcher.setattr(path_type, "read_text", denied)
            with pytest.raises(ValueError):
                app_module.mutate_users(lambda users: users.clear(), strict=True)
        assert app_module.USERS_FILE.read_bytes() == original

    def test_strict_invalid_encoding_preserves_file(self, tmp_env):
        app_module.USERS_FILE.write_bytes(b"\xff\xfe")
        with pytest.raises(ValueError):
            app_module.mutate_users(lambda users: users.clear(), strict=True)
        assert app_module.USERS_FILE.read_bytes() == b"\xff\xfe"

    def test_strict_raises_on_malformed_file(self, tmp_env, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "USERS_FILE", app_module.USERS_FILE.parent / "strict_users.json")
        app_module.USERS_FILE.write_text("{invalid json")

        with pytest.raises(ValueError):
            app_module.mutate_users(lambda u: None, strict=True)

    def test_strict_missing_file_initializes(self, tmp_env, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "USERS_FILE", app_module.USERS_FILE.parent / "strict_users.json")
        # Should not raise; missing file is OK
        result = app_module.mutate_users(lambda u: u, strict=True)
        assert result == {}

    def test_non_strict_malformed_returns_empty(self, tmp_env, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "USERS_FILE", app_module.USERS_FILE.parent / "strict_users.json")
        app_module.USERS_FILE.write_text("{invalid json")

        # Non-strict: lenient, returns {}
        result = app_module.mutate_users(lambda u: u)
        assert result == {}

    def test_persistence_error_on_save_failure(self, tmp_env, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "USERS_FILE", app_module.USERS_FILE.parent / "strict_users.json")

        def _failing_save(users):
            raise OSError("disk full")

        monkeypatch.setattr(app_module, "save_users_atomic", _failing_save)

        with pytest.raises(app_module.PersistenceError):
            app_module.mutate_users(lambda u: u, strict=True)

    def test_returns_mutator_result(self, tmp_env, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, "USERS_FILE", app_module.USERS_FILE.parent / "strict_users.json")

        def _mutator(users):
            users["test"] = {"role": "viewer"}
            return "mutated"

        result = app_module.mutate_users(_mutator, strict=True)
        assert result == "mutated"
