import base64
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


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


def _extract_session_id(response) -> str:
    """Extract the session ID value from a Set-Cookie header."""
    set_cookie = response.headers.get("set-cookie", "")
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith(app.SESSION_COOKIE_NAME + "="):
            return part[len(app.SESSION_COOKIE_NAME) + 1:]
    raise AssertionError(f"No {app.SESSION_COOKIE_NAME} cookie in Set-Cookie: {set_cookie!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_session_state():
    """Reset the in-memory session store and login rate limiter between tests."""
    with app._session_lock:
        app._sessions.clear()
        app._login_attempts.clear()
    yield
    with app._session_lock:
        app._sessions.clear()
        app._login_attempts.clear()


@pytest.fixture
def users_file(tmp_path: Path) -> Path:
    uf = tmp_path / "users.json"
    uf.write_text(
        app.json.dumps({
            "admin": _user_record("admin-pass", "admin"),
            "reader": _user_record("reader-pass", "viewer"),
        }),
        encoding="utf-8",
    )
    return uf


@pytest.fixture
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def templates_file(tmp_path: Path) -> Path:
    tf = tmp_path / "templates.json"
    tf.write_text(app.json.dumps({"templates": []}), encoding="utf-8")
    return tf


# ---------------------------------------------------------------------------
# 1. Unauthenticated browser navigation: / and /index.html
# ---------------------------------------------------------------------------

def test_unauthenticated_root_browser_serves_login_page(users_file):
    """GET / with Accept: text/html and no credentials returns the login page (200)."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    # Must be the login page, not the dashboard
    assert "login" in response.text.lower()
    # Must NOT contain dashboard-specific content
    assert 'id="workflows"' not in response.text


def test_unauthenticated_index_html_browser_serves_login_page(users_file):
    """GET /index.html with Accept: text/html and no credentials returns the login page."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/index.html", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "login" in response.text.lower()
    # Must NOT be the actual dashboard index.html
    assert 'id="workflows"' not in response.text


def test_unauthenticated_root_non_browser_returns_401(users_file):
    """GET / with Accept: application/json (non-browser) returns 401 JSON."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


# ---------------------------------------------------------------------------
# 2. Unauthenticated JSON API 401
# ---------------------------------------------------------------------------

def test_unauthenticated_json_api_returns_401_with_www_authenticate(users_file):
    """GET /api/session with Accept: application/json and no credentials → 401 + WWW-Authenticate."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/api/session", headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
    assert "Basic" in response.headers["WWW-Authenticate"]
    assert response.json()["detail"] == "Authentication required"


def test_unauthenticated_json_api_no_accept_returns_401(users_file):
    """GET /api/session with no Accept header (defaults to */*) → 401."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/api/session")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


# ---------------------------------------------------------------------------
# 3. Login cookie attributes
# ---------------------------------------------------------------------------

def test_login_sets_session_cookie_with_max_age_3600(users_file):
    """A successful login sets a session cookie with Max-Age=3600, HttpOnly, SameSite=Lax, Path=/."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert app.SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    # Over plain HTTP (TestClient default), Secure must NOT be set
    assert "Secure" not in set_cookie


def test_login_rejects_invalid_credentials(users_file):
    """A login with wrong password returns 401 and sets no session cookie."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.post("/api/login", json={"username": "admin", "password": "wrong-pass"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


# ---------------------------------------------------------------------------
# 4. Session fixation resistance (rotation on re-login)
# ---------------------------------------------------------------------------

def test_session_fixation_rotation_on_relogin(users_file):
    """Re-logging-in rotates the session: the old session ID is invalidated."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        # First login
        login1 = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id_1 = _extract_session_id(login1)
        # Second login (re-authentication)
        login2 = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id_2 = _extract_session_id(login2)

    # The new session ID must differ from the old
    assert session_id_1 != session_id_2, "Session ID must rotate on re-login"

    # Old session is now invalid
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        resp_old = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id_1})
    assert resp_old.status_code == 401

    # New session is valid
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        resp_new = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id_2})
    assert resp_new.status_code == 200


def test_attacker_chosen_session_id_rejected(users_file):
    """A session ID never issued by the server is rejected."""
    fake_session_id = "a" * 64  # 64 hex chars, same length as real session IDs
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: fake_session_id})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 5. Expired session rejection
# ---------------------------------------------------------------------------

def test_expired_idle_session_rejected(users_file):
    """A session whose idle timeout has elapsed is rejected."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        login = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id = _extract_session_id(login)
        # Age the session beyond the idle timeout
        h = app._hash_session_id(session_id)
        with app._session_lock:
            app._sessions[h]["last_seen"] = time.monotonic() - app.SESSION_IDLE_TIMEOUT - 100
        # Use the expired session
        response = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id})
    assert response.status_code == 401


def test_expired_absolute_lifetime_session_rejected(users_file):
    """A session whose absolute lifetime has elapsed is rejected."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        login = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id = _extract_session_id(login)
        # Age the session beyond the absolute lifetime
        h = app._hash_session_id(session_id)
        with app._session_lock:
            app._sessions[h]["wall_created"] = time.time() - app.SESSION_ABSOLUTE_LIFETIME - 100
        response = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 6. CSRF: missing and wrong token rejection
# ---------------------------------------------------------------------------

def _login_and_get_csrf(client, username="admin", password="admin-pass"):
    """Helper: login and return (session_id, csrf_token)."""
    login = client.post("/api/login", json={"username": username, "password": password})
    assert login.status_code == 200, f"Login failed: {login.status_code} {login.text}"
    session_id = _extract_session_id(login)
    session_resp = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id})
    assert session_resp.status_code == 200
    csrf_token = session_resp.json()["csrf_token"]
    return session_id, csrf_token


def test_csrf_missing_token_rejected(users_file, templates_file):
    """A session-authenticated POST without X-CSRF-Token is rejected with 403."""
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        client = TestClient(app.app)
        session_id, _ = _login_and_get_csrf(client)
        response = client.post(
            "/api/templates",
            cookies={app.SESSION_COOKIE_NAME: session_id},
            json={"project": "firstproject", "name": "T", "spec": {"goal": "g", "acceptance": "a"}},
        )
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_csrf_wrong_token_rejected(users_file, templates_file):
    """A session-authenticated POST with a wrong X-CSRF-Token is rejected with 403."""
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        client = TestClient(app.app)
        session_id, _ = _login_and_get_csrf(client)
        response = client.post(
            "/api/templates",
            cookies={app.SESSION_COOKIE_NAME: session_id},
            headers={"X-CSRF-Token": "wrong-token-value"},
            json={"project": "firstproject", "name": "T", "spec": {"goal": "g", "acceptance": "a"}},
        )
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 7. CSRF: correct token success
# ---------------------------------------------------------------------------

def test_csrf_correct_token_allows_request(users_file, templates_file):
    """A session-authenticated POST with the correct X-CSRF-Token succeeds."""
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        client = TestClient(app.app)
        session_id, csrf_token = _login_and_get_csrf(client)
        response = client.post(
            "/api/templates",
            cookies={app.SESSION_COOKIE_NAME: session_id},
            headers={"X-CSRF-Token": csrf_token},
            json={"project": "firstproject", "name": "T", "spec": {"goal": "g", "acceptance": "a"}},
        )
    assert response.status_code == 200
    assert response.json()["name"] == "T"


# ---------------------------------------------------------------------------
# 8. Basic-auth API compatibility (bypasses CSRF)
# ---------------------------------------------------------------------------

def test_basic_auth_post_bypasses_csrf(users_file, templates_file):
    """A Basic-auth POST without X-CSRF-Token succeeds (CSRF is not applicable to Basic Auth)."""
    headers = {"Authorization": _basic_auth("admin", "admin-pass")}
    with patch.object(app, "USERS_FILE", users_file), \
         patch.object(app, "TEMPLATES_FILE", templates_file), \
         patch.object(app, "ALLOWED_PROJECTS", ["firstproject"]):
        client = TestClient(app.app)
        response = client.post(
            "/api/templates",
            headers=headers,
            json={"project": "firstproject", "name": "T", "spec": {"goal": "g", "acceptance": "a"}},
        )
    assert response.status_code == 200
    assert response.json()["name"] == "T"


def test_basic_auth_get_session_returns_null_csrf(users_file):
    """A Basic-auth GET /api/session returns csrf_token=null."""
    headers = {"Authorization": _basic_auth("admin", "admin-pass")}
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        response = client.get("/api/session", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "admin"
    assert data["role"] == "admin"
    assert data["csrf_token"] is None


# ---------------------------------------------------------------------------
# 9. Logout invalidation
# ---------------------------------------------------------------------------

def test_logout_invalidates_session(users_file):
    """After logout, the session cookie is no longer valid."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        login = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id = _extract_session_id(login)
        # Session is valid before logout
        resp_before = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id})
        assert resp_before.status_code == 200
        # Logout
        logout = client.post("/api/logout", cookies={app.SESSION_COOKIE_NAME: session_id})
        assert logout.status_code == 200
        # Session is invalid after logout
        resp_after = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_id})
        assert resp_after.status_code == 401


def test_logout_clears_cookie(users_file):
    """The logout response includes a Set-Cookie that clears the session cookie."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        login = client.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        session_id = _extract_session_id(login)
        logout = client.post("/api/logout", cookies={app.SESSION_COOKIE_NAME: session_id})
    set_cookie = logout.headers.get("set-cookie", "")
    # The cookie should be cleared (empty value or Max-Age=0)
    assert app.SESSION_COOKIE_NAME in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("=;" in set_cookie) or ("= ;" in set_cookie) or ("Expires=" in set_cookie)


# ---------------------------------------------------------------------------
# 10. Bounded session store
# ---------------------------------------------------------------------------




def test_session_store_evicts_oldest_when_full(tmp_path: Path):
    """When the session store is at capacity, the oldest session is evicted."""
    users = {
        f"user{i}": _user_record(f"pass{i}", "viewer")
        for i in range(5)
    }
    uf = tmp_path / "users.json"
    uf.write_text(app.json.dumps(users), encoding="utf-8")

    with patch.object(app, "USERS_FILE", uf), \
         patch.object(app, "MAX_SESSIONS", 3):
        client = TestClient(app.app)
        session_ids = []
        for i in range(5):
            login = client.post("/api/login", json={"username": f"user{i}", "password": f"pass{i}"})
            assert login.status_code == 200
            session_ids.append(_extract_session_id(login))

        # The store should be bounded at 3
        with app._session_lock:
            assert len(app._sessions) <= 3

        # The oldest session (user0) should have been evicted
        resp_oldest = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_ids[0]})
        assert resp_oldest.status_code == 401

        # The newest session (user4) should still be valid
        resp_newest = client.get("/api/session", cookies={app.SESSION_COOKIE_NAME: session_ids[4]})
        assert resp_newest.status_code == 200


# ---------------------------------------------------------------------------
# 11. Trusted vs spoofed forwarded HTTPS
# ---------------------------------------------------------------------------




def _asgi_login(client_host: str, forwarded_proto: str | None = None) -> dict:
    """Send a login request via ASGI with a specific client host and optional X-Forwarded-Proto."""
    import asyncio

    body = app.json.dumps({"username": "admin", "password": "admin-pass"}).encode()
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if forwarded_proto:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/login",
        "raw_path": b"/api/login",
        "query_string": b"",
        "headers": headers,
        "client": (client_host, 12345),
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


def test_trusted_loopback_forwarded_https_sets_secure(users_file):
    """X-Forwarded-Proto: https from 127.0.0.1 (trusted) → Secure cookie is set."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("127.0.0.1", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie, f"Expected Secure flag, got: {set_cookie!r}"


def test_trusted_tailscale_cgnat_forwarded_https_sets_secure(users_file):
    """X-Forwarded-Proto: https from 100.64.0.1 (Tailscale CGNAT, trusted) → Secure cookie."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("100.64.0.1", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie, f"Expected Secure flag, got: {set_cookie!r}"


def test_untrusted_peer_forwarded_https_ignored(users_file):
    """X-Forwarded-Proto: https from an untrusted peer (8.8.8.8) → Secure cookie NOT set."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("8.8.8.8", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie, f"Secure must NOT be set for untrusted peer, got: {set_cookie!r}"


def test_no_forwarded_proto_no_secure(users_file):
    """No X-Forwarded-Proto header → Secure cookie NOT set (plain HTTP)."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("127.0.0.1", None)
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


def test_https_scheme_direct_sets_secure(users_file):
    """A direct HTTPS connection (scheme=https) → Secure cookie is set regardless of forwarded headers."""
    import asyncio

    body = app.json.dumps({"username": "admin", "password": "admin-pass"}).encode()
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/login",
        "raw_path": b"/api/login",
        "query_string": b"",
        "headers": headers,
        "client": ("8.8.8.8", 12345),  # Untrusted peer, but scheme is https
        "server": ("testserver", 443),
        "scheme": "https",
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

    with patch.object(app, "USERS_FILE", users_file):
        asyncio.run(app.app(scope, receive, send))

    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie, f"Expected Secure for direct HTTPS, got: {set_cookie!r}"


# ---------------------------------------------------------------------------
# 12. Login rate-limiter stale key sweeping
# ---------------------------------------------------------------------------

def test_login_rate_limit_sweeps_stale_keys():
    """Stale client-IP keys (newest timestamp older than the window) are deleted;
    active keys (with a recent timestamp) are preserved."""
    now = time.time()
    window = app.LOGIN_RATE_LIMIT_WINDOW

    with app._login_rate_lock:
        # Stale key: newest timestamp is well outside the window
        app._login_attempts["10.0.0.1"] = [now - window - 100]
        # Active key: newest timestamp is within the window
        app._login_attempts["10.0.0.2"] = [now - 5]
        # Another stale key
        app._login_attempts["10.0.0.3"] = [now - window - 200, now - window - 150]

    # Trigger the sweep by checking a new IP
    result = app._check_login_rate_limit("10.0.0.99")
    assert result is True  # New IP is allowed

    with app._login_rate_lock:
        # Stale keys must have been swept
        assert "10.0.0.1" not in app._login_attempts
        assert "10.0.0.3" not in app._login_attempts
        # Active key must remain
        assert "10.0.0.2" in app._login_attempts
        # The new key was added
        assert "10.0.0.99" in app._login_attempts


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_login_page_contains_session_note(users_file, path):
    """GET / with Accept: text/html returns the session note and aria-describedby."""
    with patch.object(app, "USERS_FILE", users_file):
        client = TestClient(app.app)
        resp = client.get(path, headers={"Accept": "text/html"})
        assert resp.status_code == 200
        body = resp.text
        assert "Signing in ends any previous session for this account." in body
        assert 'id="login-session-note"' in body
        assert body.count('id="login-session-note"') == 1
        assert 'aria-describedby="login-session-note"' in body
