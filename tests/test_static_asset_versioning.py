"""Tests for build-time static asset versioning helper."""

import hashlib
import re
from pathlib import Path

import pytest

import version_static_assets as vsa


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_short(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _make_static_dir(tmp_path: Path, app_js: str = "console.log('app');",
                     styles_css: str = "body{color:red}",
                     index_html: str | None = None,
                     login_html: str | None = None) -> Path:
    """Create a minimal static directory with known content."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text(app_js, encoding="utf-8")
    (static / "styles.css").write_text(styles_css, encoding="utf-8")

    if index_html is None:
        index_html = (
            '<html><head>\n'
            '  <link rel="stylesheet" href="/styles.css">\n'
            '</head><body>\n'
            '  <script src="/app.js?v=reliability-counts-3" defer></script>\n'
            '</body></html>\n'
        )
    (static / "index.html").write_text(index_html, encoding="utf-8")

    if login_html is None:
        login_html = (
            '<html><head>\n'
            '  <link rel="stylesheet" href="/styles.css">\n'
            '</head><body>\n'
            '  <form action="/api/login"></form>\n'
            '</body></html>\n'
        )
    (static / "login.html").write_text(login_html, encoding="utf-8")

    return static


# ---------------------------------------------------------------------------
# 1. Exact content digest URL
# ---------------------------------------------------------------------------

def test_exact_content_digest_url(tmp_path):
    """The ?v= value in the output must equal the SHA256 short hash of the file content."""
    app_js = "console.log('hello world');"
    styles_css = "body { margin: 0; }"
    static = _make_static_dir(tmp_path, app_js=app_js, styles_css=styles_css)

    vsa.version_static_assets(static)

    index = (static / "index.html").read_text()
    expected_js_hash = _sha256_short(app_js.encode())
    expected_css_hash = _sha256_short(styles_css.encode())

    assert f"/app.js?v={expected_js_hash}" in index
    assert f"/styles.css?v={expected_css_hash}" in index


# ---------------------------------------------------------------------------
# 2. Unchanged content stable (same hash on re-run)
# ---------------------------------------------------------------------------

def test_unchanged_content_stable(tmp_path):
    """If file content doesn't change, the versioned URL stays the same across runs."""
    app_js = "var x = 1;"
    styles_css = "p { color: blue; }"
    static = _make_static_dir(tmp_path, app_js=app_js, styles_css=styles_css)

    vsa.version_static_assets(static)
    first_index = (static / "index.html").read_text()
    first_login = (static / "login.html").read_text()

    # Run again with same content
    vsa.version_static_assets(static)
    second_index = (static / "index.html").read_text()
    second_login = (static / "login.html").read_text()

    assert first_index == second_index
    assert first_login == second_login


# ---------------------------------------------------------------------------
# 3. Modified JS changes JS URL only
# ---------------------------------------------------------------------------

def test_modified_js_changes_js_url_only(tmp_path):
    """Modifying app.js changes only the app.js URL; styles.css URL is unaffected."""
    app_js_v1 = "console.log('v1');"
    styles_css = "body { margin: 0; }"
    static = _make_static_dir(tmp_path, app_js=app_js_v1, styles_css=styles_css)

    vsa.version_static_assets(static)
    index_v1 = (static / "index.html").read_text()

    css_hash = _sha256_short(styles_css.encode())
    js_hash_v1 = _sha256_short(app_js_v1.encode())

    # Modify app.js
    app_js_v2 = "console.log('v2');"
    (static / "app.js").write_text(app_js_v2, encoding="utf-8")

    vsa.version_static_assets(static)
    index_v2 = (static / "index.html").read_text()

    js_hash_v2 = _sha256_short(app_js_v2.encode())
    assert js_hash_v1 != js_hash_v2

    # JS URL changed
    assert f"/app.js?v={js_hash_v1}" in index_v1
    assert f"/app.js?v={js_hash_v2}" in index_v2
    assert f"/app.js?v={js_hash_v1}" not in index_v2

    # CSS URL unchanged
    assert f"/styles.css?v={css_hash}" in index_v1
    assert f"/styles.css?v={css_hash}" in index_v2


# ---------------------------------------------------------------------------
# 4. Modified CSS changes both pages CSS URL
# ---------------------------------------------------------------------------

def test_modified_css_changes_both_pages_css_url(tmp_path):
    """Modifying styles.css updates the CSS URL in both index.html and login.html."""
    app_js = "console.log('app');"
    css_v1 = "body { color: red; }"
    static = _make_static_dir(tmp_path, app_js=app_js, styles_css=css_v1)

    vsa.version_static_assets(static)
    index_v1 = (static / "index.html").read_text()
    login_v1 = (static / "login.html").read_text()

    css_hash_v1 = _sha256_short(css_v1.encode())
    assert f"/styles.css?v={css_hash_v1}" in index_v1
    assert f"/styles.css?v={css_hash_v1}" in login_v1

    # Modify CSS
    css_v2 = "body { color: blue; }"
    (static / "styles.css").write_text(css_v2, encoding="utf-8")

    vsa.version_static_assets(static)
    index_v2 = (static / "index.html").read_text()
    login_v2 = (static / "login.html").read_text()

    css_hash_v2 = _sha256_short(css_v2.encode())
    assert css_hash_v1 != css_hash_v2

    assert f"/styles.css?v={css_hash_v2}" in index_v2
    assert f"/styles.css?v={css_hash_v2}" in login_v2
    assert f"/styles.css?v={css_hash_v1}" not in index_v2
    assert f"/styles.css?v={css_hash_v1}" not in login_v2


# ---------------------------------------------------------------------------
# 5. Re-run idempotent
# ---------------------------------------------------------------------------

def test_rerun_idempotent(tmp_path):
    """Running the helper twice produces identical output (no double-query or drift)."""
    static = _make_static_dir(tmp_path)

    vsa.version_static_assets(static)
    first = (static / "index.html").read_text()

    vsa.version_static_assets(static)
    second = (static / "index.html").read_text()

    assert first == second
    # No double query strings
    assert "?v=" not in first.replace("?v=", "", 1) or first.count("?v=") == first.count("?v=")
    # Ensure no ?v=...&v=... pattern
    assert not re.search(r"\?v=[^\"']+&v=", first)


# ---------------------------------------------------------------------------
# 6. Missing file failure
# ---------------------------------------------------------------------------

def test_missing_file_failure(tmp_path):
    """If a required asset file is missing, the helper raises FileNotFoundError."""
    static = tmp_path / "static"
    static.mkdir()
    # Only create styles.css, omit app.js
    (static / "styles.css").write_text("body{}", encoding="utf-8")
    (static / "index.html").write_text('<html><link href="/styles.css"></html>', encoding="utf-8")
    (static / "login.html").write_text('<html><link href="/styles.css"></html>', encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="app\\.js"):
        vsa.version_static_assets(static)


def test_missing_html_file_failure(tmp_path):
    """If a required HTML file is missing, the helper raises FileNotFoundError."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("x", encoding="utf-8")
    (static / "styles.css").write_text("y", encoding="utf-8")
    (static / "index.html").write_text('<html></html>', encoding="utf-8")
    # login.html is missing

    with pytest.raises(FileNotFoundError, match="login\\.html"):
        vsa.version_static_assets(static)


# ---------------------------------------------------------------------------
# 7. Script doesn't modify JS/CSS content
# ---------------------------------------------------------------------------

def test_script_does_not_modify_js_or_css(tmp_path):
    """The helper only rewrites HTML files; JS and CSS file contents are untouched."""
    app_js = "console.log('original');"
    styles_css = "body { margin: 0; }"
    static = _make_static_dir(tmp_path, app_js=app_js, styles_css=styles_css)

    vsa.version_static_assets(static)

    assert (static / "app.js").read_text() == app_js
    assert (static / "styles.css").read_text() == styles_css


# ---------------------------------------------------------------------------
# 8. Runtime app.py untouched
# ---------------------------------------------------------------------------

def test_app_py_untouched(tmp_path):
    """The helper does not modify app.py or any file outside the static directory."""
    static = _make_static_dir(tmp_path)
    app_py = tmp_path / "app.py"
    app_py.write_text("# app code\n", encoding="utf-8")

    vsa.version_static_assets(static)

    assert app_py.read_text() == "# app code\n"


# ---------------------------------------------------------------------------
# 9. Old hardcoded query replaced
# ---------------------------------------------------------------------------

def test_old_hardcoded_query_replaced(tmp_path):
    """An existing ?v=reliability-counts-3 query is replaced with the content hash."""
    app_js = "console.log('v3');"
    static = _make_static_dir(tmp_path, app_js=app_js)

    vsa.version_static_assets(static)

    index = (static / "index.html").read_text()
    # Old hardcoded value must be gone
    assert "reliability-counts-3" not in index
    # New hash must be present
    expected_hash = _sha256_short(app_js.encode())
    assert f"/app.js?v={expected_hash}" in index


# ---------------------------------------------------------------------------
# 10. Dockerfile invokes helper AFTER COPY and copies helper
# ---------------------------------------------------------------------------

def test_dockerfile_invokes_helper_after_copy():
    """The Dockerfile must COPY the helper and RUN it after COPY static."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    lines = dockerfile.splitlines()

    # Find line indices
    copy_static_idx = None
    copy_helper_idx = None
    run_helper_idx = None

    for i, line in enumerate(lines):
        if "COPY static" in line:
            copy_static_idx = i
        if "COPY version_static_assets.py" in line:
            copy_helper_idx = i
        if "RUN python version_static_assets.py" in line:
            run_helper_idx = i

    assert copy_static_idx is not None, "Dockerfile must COPY static"
    assert copy_helper_idx is not None, "Dockerfile must COPY version_static_assets.py"
    assert run_helper_idx is not None, "Dockerfile must RUN the helper"
    # Helper must run AFTER static is copied
    assert run_helper_idx > copy_static_idx, "Helper must run after COPY static"
    # Helper must be copied before it runs
    assert copy_helper_idx < run_helper_idx, "Helper must be copied before running"


# ---------------------------------------------------------------------------
# 11. CLI: safe with optional arg
# ---------------------------------------------------------------------------

def test_cli_missing_dir_returns_nonzero(tmp_path):
    """CLI with a non-existent directory returns exit code 1."""
    rc = vsa.main([str(tmp_path / "nonexistent")])
    assert rc == 1


def test_cli_valid_dir_returns_zero(tmp_path):
    """CLI with a valid static dir returns exit code 0."""
    static = _make_static_dir(tmp_path)
    rc = vsa.main([str(static)])
    assert rc == 0


# ---------------------------------------------------------------------------
# 12. Hash length >= 12 hex chars
# ---------------------------------------------------------------------------

def test_hash_length_at_least_12_hex(tmp_path):
    """The version hash must be at least 12 hex characters."""
    static = _make_static_dir(tmp_path)
    vsa.version_static_assets(static)
    index = (static / "index.html").read_text()

    # Extract all ?v= values
    hashes = re.findall(r"\?v=([0-9a-f]+)", index)
    assert len(hashes) >= 2  # at least app.js and styles.css
    for h in hashes:
        assert len(h) >= 12
        assert all(c in "0123456789abcdef" for c in h)
