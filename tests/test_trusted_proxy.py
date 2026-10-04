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


def _asgi_login(client_host: str, forwarded_proto: str | None = None, scheme: str = "http") -> dict:
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
        "scheme": scheme,
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


# ---------------------------------------------------------------------------
# 1. _is_trusted_proxy: exact trusted proxy IP
# ---------------------------------------------------------------------------

def test_trusted_proxy_ip_is_trusted():
    """The exact TRUSTED_PROXY_IP (172.28.0.2) is trusted."""
    assert app._is_trusted_proxy(app.TRUSTED_PROXY_IP) is True


def test_trusted_proxy_ip_default_value():
    """The default TRUSTED_PROXY_IP is 172.28.0.2 (the Caddy container on the dedicated network)."""
    assert app.TRUSTED_PROXY_IP == "172.28.0.2"


# ---------------------------------------------------------------------------
# 2. _is_trusted_proxy: loopback
# ---------------------------------------------------------------------------

def test_loopback_ipv4_trusted():
    assert app._is_trusted_proxy("127.0.0.1") is True


def test_loopback_ipv6_trusted():
    assert app._is_trusted_proxy("::1") is True


# ---------------------------------------------------------------------------
# 3. _is_trusted_proxy: Tailscale CGNAT range
# ---------------------------------------------------------------------------

def test_tailscale_cgnat_low_trusted():
    """100.64.0.0 is the start of the CGNAT range (100.64.0.0/10)."""
    assert app._is_trusted_proxy("100.64.0.1") is True


def test_tailscale_cgnat_high_trusted():
    """100.127.255.255 is the end of the CGNAT range."""
    assert app._is_trusted_proxy("100.127.255.255") is True


def test_tailscale_cgnat_boundary_63_not_trusted():
    """100.63.x.x is just below the CGNAT range (second octet < 64)."""
    assert app._is_trusted_proxy("100.63.0.1") is False


def test_tailscale_cgnat_boundary_128_not_trusted():
    """100.128.x.x is just above the CGNAT range (second octet > 127)."""
    assert app._is_trusted_proxy("100.128.0.1") is False


def test_non_cgnat_100_prefix_not_trusted():
    """100.1.0.1 starts with 100. but is not in the CGNAT range."""
    assert app._is_trusted_proxy("100.1.0.1") is False


# ---------------------------------------------------------------------------
# 4. _is_trusted_proxy: other Docker peers (NOT trusted)
# ---------------------------------------------------------------------------

def test_docker_bridge_gateway_not_trusted():
    """The default Docker bridge gateway (172.17.0.1) is NOT trusted.

    Normal LAN clients reaching the published port 192.168.68.68:8088 appear
    as the Docker bridge gateway IP, which must NOT be in the trusted list.
    """
    assert app._is_trusted_proxy("172.17.0.1") is False


def test_other_docker_network_ip_not_trusted():
    """A different IP on the dashboard-tls-proxy network (e.g., 172.28.0.3) is NOT trusted.

    Only the exact fixed proxy address (172.28.0.2) is trusted; other peers
    on the same Docker network cannot spoof the proxy identity.
    """
    assert app._is_trusted_proxy("172.28.0.3") is False


def test_docker_network_gateway_not_trusted():
    """The gateway of the dashboard-tls-proxy network (172.28.0.1) is NOT trusted."""
    assert app._is_trusted_proxy("172.28.0.1") is False


def test_m1_agent_repo_chat_network_ip_not_trusted():
    """An IP from the m1-agent-repo_chat external network is NOT trusted."""
    assert app._is_trusted_proxy("172.18.0.5") is False


# ---------------------------------------------------------------------------
# 5. _is_trusted_proxy: LAN spoofing (NOT trusted)
# ---------------------------------------------------------------------------

def test_lan_client_ip_not_trusted():
    """A normal LAN client IP (192.168.68.x) is NOT trusted."""
    assert app._is_trusted_proxy("192.168.68.100") is False


def test_lan_gateway_not_trusted():
    """The LAN gateway (192.168.68.1) is NOT trusted."""
    assert app._is_trusted_proxy("192.168.68.1") is False


def test_private_ethernet_ip_not_trusted():
    """The M1's private Ethernet IP (10.10.10.2) is NOT trusted.

    The Caddy proxy container's source IP on the Docker network is 172.28.0.2,
    not the host's Ethernet IP. A direct connection from the host's Ethernet
    interface would appear as 10.10.10.2 and must NOT be trusted.
    """
    assert app._is_trusted_proxy("10.10.10.2") is False


def test_public_ip_not_trusted():
    """A public IP is NOT trusted."""
    assert app._is_trusted_proxy("8.8.8.8") is False


def test_localhost_hostname_not_trusted():
    """A hostname string is NOT trusted (only exact IP strings match)."""
    assert app._is_trusted_proxy("localhost") is False


def test_empty_string_not_trusted():
    """An empty string (no client) is NOT trusted."""
    assert app._is_trusted_proxy("") is False


# ---------------------------------------------------------------------------
# 6. _is_trusted_https: integration via ASGI login
# ---------------------------------------------------------------------------

def test_login_from_trusted_proxy_sets_secure_cookie(users_file):
    """Login from the trusted proxy IP (172.28.0.2) with X-Forwarded-Proto: https
    sets the Secure cookie flag."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie, f"Expected Secure flag from trusted proxy, got: {set_cookie!r}"


def test_login_from_loopback_sets_secure_cookie(users_file):
    """Login from 127.0.0.1 with X-Forwarded-Proto: https sets Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("127.0.0.1", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie


def test_login_from_tailscale_cgnat_sets_secure_cookie(users_file):
    """Login from a Tailscale CGNAT IP with X-Forwarded-Proto: https sets Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("100.64.1.1", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie


def test_login_from_lan_client_no_secure_cookie(users_file):
    """Login from a LAN client IP (192.168.68.100) with spoofed X-Forwarded-Proto: https
    does NOT set the Secure cookie flag. The LAN client cannot spoof trust."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("192.168.68.100", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie, (
        f"Secure must NOT be set for LAN client spoofing, got: {set_cookie!r}"
    )


def test_login_from_docker_bridge_gateway_no_secure_cookie(users_file):
    """Login from the Docker bridge gateway (172.17.0.1) with spoofed
    X-Forwarded-Proto: https does NOT set Secure. Normal LAN clients reaching
    the published port appear as this IP and must not be trusted."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("172.17.0.1", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie, (
        f"Secure must NOT be set for Docker bridge gateway, got: {set_cookie!r}"
    )


def test_login_from_other_docker_peer_no_secure_cookie(users_file):
    """Login from another peer on the same Docker network (172.28.0.3) with
    spoofed X-Forwarded-Proto: https does NOT set Secure. Only the exact
    fixed proxy IP (172.28.0.2) is trusted."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("172.28.0.3", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie, (
        f"Secure must NOT be set for other Docker peer, got: {set_cookie!r}"
    )


def test_login_from_public_ip_no_secure_cookie(users_file):
    """Login from a public IP with spoofed X-Forwarded-Proto: https does NOT set Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("8.8.8.8", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


# ---------------------------------------------------------------------------
# 7. _is_trusted_https: missing/malformed forwarded proto
# ---------------------------------------------------------------------------

def test_no_forwarded_proto_header_no_secure(users_file):
    """No X-Forwarded-Proto header from a trusted proxy → no Secure flag."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, None)
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


def test_malformed_forwarded_proto_http_no_secure(users_file):
    """X-Forwarded-Proto: http from a trusted proxy → no Secure flag."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, "http")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


def test_malformed_forwarded_proto_garbage_no_secure(users_file):
    """X-Forwarded-Proto: garbage from a trusted proxy → no Secure flag."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, "garbage-value")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


def test_forwarded_proto_https_from_untrusted_no_secure(users_file):
    """X-Forwarded-Proto: https from an untrusted peer → no Secure flag."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("192.168.68.100", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie


# ---------------------------------------------------------------------------
# 8. Direct HTTPS (scheme=https) always sets Secure
# ---------------------------------------------------------------------------

def test_direct_https_scheme_sets_secure_even_from_untrusted(users_file):
    """A direct HTTPS connection (scheme=https) sets Secure regardless of
    the client IP or forwarded headers. This covers the case where the
    application itself terminates TLS."""
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
        "client": ("8.8.8.8", 12345),  # Untrusted peer
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
# 9. Cookie attributes: full contract
# ---------------------------------------------------------------------------

def test_login_cookie_full_attributes_plain_http(users_file):
    """Over plain HTTP from an untrusted peer: HttpOnly, SameSite=Lax, Path=/,
    Max-Age=3600, NO Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("192.168.68.100", None)
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert app.SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert "Secure" not in set_cookie


def test_login_cookie_full_attributes_https_via_proxy(users_file):
    """Over HTTP from the trusted proxy with X-Forwarded-Proto: https:
    HttpOnly, SameSite=Lax, Path=/, Max-Age=3600, Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert app.SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=3600" in set_cookie
    assert "Secure" in set_cookie


# ---------------------------------------------------------------------------
# 10. Compose.yaml: proxy service validation
# ---------------------------------------------------------------------------

def test_compose_has_dashboard_tls_proxy_service():
    """compose.yaml must define a dashboard-tls-proxy service."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "dashboard-tls-proxy:" in compose


def test_compose_proxy_port_bound_to_private_ethernet():
    """The proxy port must be bound only to 10.10.10.2 (private direct Ethernet),
    NOT to 0.0.0.0 or the LAN interface."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    # The port binding must specify 10.10.10.2 as the host interface
    assert "10.10.10.2:8444:8444" in compose
    # Must NOT be bound to 0.0.0.0 or the LAN IP
    assert "0.0.0.0:8444" not in compose
    assert "192.168.68.68:8444" not in compose


def test_compose_proxy_on_dedicated_network():
    """The proxy must be on the dedicated dashboard-tls-proxy network with a fixed IP."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "dashboard-tls-proxy" in compose
    assert "172.28.0.2" in compose
    assert "172.28.0.0/16" in compose


def test_compose_dashboard_on_proxy_network():
    """The dashboard must also join the dashboard-tls-proxy network so the proxy can reach it."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    # The dashboard service must list dashboard-tls-proxy in its networks
    import re
    dashboard_match = re.search(r"^  dashboard:\n(.*?)(?=^  \S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert dashboard_match is not None
    assert "dashboard-tls-proxy" in dashboard_match.group(0)


def test_compose_trusted_proxy_ip_env():
    """The dashboard must receive TRUSTED_PROXY_IP=172.28.0.2 as an environment variable."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "TRUSTED_PROXY_IP: \"172.28.0.2\"" in compose or "TRUSTED_PROXY_IP: 172.28.0.2" in compose


def test_compose_proxy_healthcheck_is_http_probe():
    """The proxy healthcheck must be an HTTP end-to-end probe through Caddy
    to the dashboard /health endpoint, not a syntax-only check."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    import re
    proxy_match = re.search(r"^  dashboard-tls-proxy:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert proxy_match is not None
    block = proxy_match.group(0)
    assert "healthcheck" in block
    # Must probe the /health endpoint through Caddy's own listener
    assert "/health" in block
    # Must NOT be a syntax-only check
    assert "caddy validate" not in block


def test_compose_proxy_depends_on_dashboard_healthy():
    """The proxy must depend on the dashboard being healthy (not just started)."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    import re
    proxy_match = re.search(r"^  dashboard-tls-proxy:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert proxy_match is not None
    block = proxy_match.group(0)
    assert "condition: service_healthy" in block


def test_compose_proxy_caddyfile_mounted():
    """The proxy must mount the Caddyfile."""
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in compose


# ---------------------------------------------------------------------------
# 11. Caddyfile: configuration validation
# ---------------------------------------------------------------------------

def test_caddyfile_listens_on_8444():
    """The Caddyfile must listen on port 8444 (backend), not 8443 (external Tailscale)."""
    caddyfile = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    assert ":8444" in caddyfile
    assert ":8443" not in caddyfile


def test_caddyfile_reverse_proxies_to_dashboard():
    """The Caddyfile must reverse-proxy to dashboard:8080."""
    caddyfile = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy dashboard:8080" in caddyfile


def test_caddyfile_sets_forwarded_proto_https():
    """The Caddyfile must set X-Forwarded-Proto to https."""
    caddyfile = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    assert "X-Forwarded-Proto https" in caddyfile or "X-Forwarded-Proto: https" in caddyfile


def test_caddyfile_no_custom_auth_header():
    """The Caddyfile must NOT inject any custom X-Proxy-Auth or shared-secret header.

    The trust boundary is enforced by interface binding + Docker network,
    not by a shared secret.
    """
    caddyfile = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    assert "X-Proxy-Auth" not in caddyfile
    assert "X-Auth" not in caddyfile
    assert "X-Shared-Secret" not in caddyfile


def test_caddyfile_documents_tailscale_on_m5():
    """The Caddyfile must document that Tailscale Serve runs on the M5,
    not on the M1. It must not claim Tailscale runs on the M1."""
    caddyfile = (Path(__file__).resolve().parent.parent / "Caddyfile").read_text(encoding="utf-8")
    # Must mention M5 as the Tailscale Serve host
    assert "M5" in caddyfile
    # Must not claim Tailscale Serve runs on the M1
    # (The M1 is the Caddy host; Tailscale is on the M5)
    assert "Tailscale Serve on the M1" not in caddyfile
    assert "Tailscale Serve (on the M1" not in caddyfile


# ---------------------------------------------------------------------------
# 12. No shared-secret header in app.py
# ---------------------------------------------------------------------------

def test_app_no_shared_secret_header_trust():
    """The app must NOT use any shared-secret header (X-Proxy-Auth, X-Auth-Token, etc.)
    for trust determination. Trust is based solely on source IP."""
    source = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    # The _is_trusted_https function must not check any auth/secret headers
    assert "x-proxy-auth" not in source.lower()
    assert "x-auth-token" not in source.lower()
    assert "x-shared-secret" not in source.lower()


# ---------------------------------------------------------------------------
# 13. README: Tailscale Serve CLI documentation regression
# ---------------------------------------------------------------------------

def test_readme_no_backend_flag():
    """The README must NOT document the nonexistent --backend flag for
    tailscale serve. This is a regression guard against reintroducing
    invalid CLI syntax."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "--backend" not in readme, (
        "README documents '--backend' which is not a valid tailscale serve flag"
    )


def test_readme_has_supported_serve_syntax():
    """The README must document the supported tailscale serve syntax:
    tailscale serve --bg --https=8443 http://10.10.10.2:8444"""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "tailscale serve --bg --https=8443 http://10.10.10.2:8444" in readme, (
        "README must document external HTTPS 8443 forwarding to backend port 8444"
    )


def test_readme_has_serve_status_verification():
    """The README must include a 'tailscale serve status' verification command."""
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "tailscale serve status" in readme, (
        "README must include 'tailscale serve status' as a verification command"
    )


# ---------------------------------------------------------------------------
# 14. Comprehensive spoofing matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client_ip,expected_trusted", [
    # Trusted sources
    ("127.0.0.1", True),       # Loopback IPv4
    ("::1", True),             # Loopback IPv6
    ("172.28.0.2", True),      # The exact Caddy proxy IP
    ("100.64.0.1", True),      # Tailscale CGNAT low
    ("100.127.255.255", True), # Tailscale CGNAT high
    # Untrusted sources
    ("172.17.0.1", False),     # Default Docker bridge gateway
    ("172.28.0.1", False),     # Proxy network gateway
    ("172.28.0.3", False),     # Other peer on proxy network
    ("192.168.68.100", False), # LAN client
    ("192.168.68.1", False),   # LAN gateway
    ("10.10.10.2", False),     # M1 private Ethernet (host, not container)
    ("8.8.8.8", False),        # Public IP
    ("100.1.0.1", False),      # 100.x but not CGNAT
    ("100.63.0.1", False),     # Just below CGNAT range
    ("100.128.0.1", False),    # Just above CGNAT range
    ("", False),               # Empty
    ("localhost", False),      # Hostname
])
def test_trust_matrix(client_ip, expected_trusted):
    """Comprehensive trust matrix: each IP must yield the expected trust result."""
    assert app._is_trusted_proxy(client_ip) is expected_trusted, (
        f"_is_trusted_proxy({client_ip!r}) = {app._is_trusted_proxy(client_ip)}, "
        f"expected {expected_trusted}"
    )


# ---------------------------------------------------------------------------
# 15. End-to-end: LAN client cannot spoof Secure cookie
# ---------------------------------------------------------------------------

def test_e2e_lan_client_cannot_spoof_secure(users_file):
    """End-to-end: a LAN client (192.168.68.100) sending X-Forwarded-Proto: https
    to the dashboard's published port must NOT get a Secure cookie.

    This is the critical security property: normal LAN users reaching
    192.168.68.68:8088 cannot spoof the HTTPS trust boundary.
    """
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("192.168.68.100", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" not in set_cookie
    # The session is still created (login succeeds), just without Secure
    assert app.SESSION_COOKIE_NAME in set_cookie


def test_e2e_tailscale_path_gets_secure(users_file):
    """End-to-end: the Tailscale path (via Caddy proxy at 172.28.0.2)
    with X-Forwarded-Proto: https gets a Secure cookie."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login(app.TRUSTED_PROXY_IP, "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie
    assert app.SESSION_COOKIE_NAME in set_cookie


def test_e2e_direct_tailscale_cgnat_gets_secure(users_file):
    """End-to-end: direct Tailscale Serve on the host (CGNAT source) gets Secure."""
    with patch.object(app, "USERS_FILE", users_file):
        result = _asgi_login("100.64.1.42", "https")
    assert result["status"] == 200
    set_cookie = result["headers"].get("set-cookie", "")
    assert "Secure" in set_cookie
