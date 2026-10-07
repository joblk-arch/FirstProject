"""Tests for the secure user storage foundation in app.py."""

import hashlib
import hmac
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import (
    PASSWORD_ALGORITHM,
    PASSWORD_ITERATIONS,
    PASSWORD_SALT_BYTES,
    USERS_FILE,
    PASSWORD_FILE,
    _users_lock,
    _session_lock,
    _sessions,
    create_password_record,
    load_users,
    load_users_unlocked,
    load_users_unlocked_strict,
    mutate_users,
    revoke_user_sessions,
    save_users_atomic,
    authenticate_identity,
    _create_session,
    _destroy_session,
    _validate_session,
    _verify_password,
    VALID_ROLES,
)
from fastapi.security import HTTPBasicCredentials


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_users_file(tmp_path, monkeypatch):
    """Redirect USERS_FILE and PASSWORD_FILE to temp paths so tests never touch live data."""
    fake_users = tmp_path / "users.json"
    fake_pw = tmp_path / "passwords.json"
    monkeypatch.setattr("app.USERS_FILE", fake_users)
    monkeypatch.setattr("app.PASSWORD_FILE", fake_pw)
    with _session_lock:
        _sessions.clear()
    yield
    with _session_lock:
        _sessions.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user_in_file(username, password, role="viewer", disabled=None):
    """Write a user directly into the users file."""
    record = create_password_record(password, role)
    if disabled is not None:
        record["disabled"] = disabled
    users = {username: record}
    with _users_lock:
        save_users_atomic(users)
    return record


def _write_users_file(users: dict, tmp_path, monkeypatch):
    """Write a users dict to the isolated users file."""
    fake = tmp_path / "users.json"
    fake.write_text(json.dumps(users), encoding="utf-8")
    monkeypatch.setattr("app.USERS_FILE", fake)


# ---------------------------------------------------------------------------
# Tests: create_password_record
# ---------------------------------------------------------------------------

class TestCreatePasswordRecord:
    def test_returns_dict_with_expected_keys(self):
        rec = create_password_record("mypassword", "admin")
        assert isinstance(rec, dict)
        assert "password_hash" in rec
        assert "salt" in rec
        assert "role" in rec
        assert "iterations" in rec
        assert rec["role"] == "admin"

    def test_hash_and_salt_are_hex(self):
        rec = create_password_record("test", "viewer")
        # Should decode from hex without error
        h = bytes.fromhex(rec["password_hash"])
        s = bytes.fromhex(rec["salt"])
        assert len(s) == PASSWORD_SALT_BYTES
        assert len(h) == 32  # SHA-256 digest length

    def test_uses_correct_iterations(self):
        rec = create_password_record("test", "viewer")
        assert rec["iterations"] == PASSWORD_ITERATIONS
        salt = bytes.fromhex(rec["salt"])
        expected = hashlib.pbkdf2_hmac(PASSWORD_ALGORITHM, b"test", salt, PASSWORD_ITERATIONS)
        assert bytes.fromhex(rec["password_hash"]) == expected

    def test_different_salts_for_same_password(self):
        r1 = create_password_record("same", "viewer")
        r2 = create_password_record("same", "viewer")
        assert r1["salt"] != r2["salt"]
        assert r1["password_hash"] != r2["password_hash"]

    def test_role_is_stored(self):
        rec = create_password_record("pw", "operator")
        assert rec["role"] == "operator"

    def test_compatible_with_verify_password(self):
        rec = create_password_record("s3cret", "admin")
        assert _verify_password("s3cret", rec) is True
        assert _verify_password("wrong", rec) is False


# ---------------------------------------------------------------------------
# Tests: load_users / load_users_unlocked
# ---------------------------------------------------------------------------

class TestLoadUsers:
    def test_returns_empty_dict_when_file_missing(self):
        result = load_users()
        assert result == {}

    def test_returns_empty_dict_on_malformed_json(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text("not valid json{{{", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert result == {}

    def test_returns_empty_dict_when_top_level_not_dict(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert result == {}

    def test_drops_records_missing_role(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        data = {
            "good": {"role": "admin", "password_hash": "aa", "salt": "bb"},
            "bad": {"password_hash": "aa", "salt": "bb"},  # no role
        }
        fake.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert "good" in result
        assert "bad" not in result

    def test_drops_non_dict_values(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        data = {"good": {"role": "admin"}, "bad": "not a dict"}
        fake.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert "good" in result
        assert "bad" not in result

    def test_drops_invalid_role(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        data = {"good": {"role": "admin"}, "bad": {"role": "superuser"}}
        fake.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert "good" in result
        assert "bad" not in result

    def test_valid_record_preserved(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        data = {"alice": {"role": "admin", "password_hash": "aabb", "salt": "ccdd"}}
        fake.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users()
        assert result["alice"]["role"] == "admin"

    def test_load_users_unlocked_does_not_deadlock(self, tmp_path, monkeypatch):
        """load_users_unlocked should work even if lock is held (no deadlock)."""
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"u": {"role": "viewer"}}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        with _users_lock:
            result = load_users_unlocked()
        assert "u" in result


# ---------------------------------------------------------------------------
# Tests: save_users_atomic
# ---------------------------------------------------------------------------

class TestSaveUsersAtomic:
    def test_writes_valid_json(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake)
        users = {"alice": {"role": "admin", "password_hash": "x", "salt": "y"}}
        with _users_lock:
            save_users_atomic(users)
        data = json.loads(fake.read_text(encoding="utf-8"))
        assert data == users

    def test_file_mode_is_0600(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake)
        users = {"alice": {"role": "admin"}}
        with _users_lock:
            save_users_atomic(users)
        mode = fake.stat().st_mode & 0o777
        assert mode == 0o600

    def test_no_temp_file_left_on_success(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake)
        users = {"alice": {"role": "admin"}}
        with _users_lock:
            save_users_atomic(users)
        remaining = [f for f in tmp_path.iterdir() if f.name != "users.json"]
        assert remaining == []

    def test_temp_file_cleaned_on_replace_failure(self, tmp_path, monkeypatch):
        """If os.replace fails, the temp file must be unlinked."""
        fake = tmp_path / "users.json"
        fake.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        original_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("simulated failure")

        monkeypatch.setattr("os.replace", failing_replace)
        with _users_lock:
            with pytest.raises(OSError):
                save_users_atomic({"alice": {"role": "admin"}})

        # Temp file should have been cleaned up
        remaining = [f for f in tmp_path.iterdir() if f.name != "users.json"]
        assert remaining == []

    def test_original_file_unchanged_on_failure(self, tmp_path, monkeypatch):
        """On replace failure, the original file must remain intact."""
        fake = tmp_path / "users.json"
        original_content = json.dumps({"original": {"role": "admin"}})
        fake.write_text(original_content, encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        def failing_replace(src, dst):
            raise OSError("simulated")

        monkeypatch.setattr("os.replace", failing_replace)
        with _users_lock:
            with pytest.raises(OSError):
                save_users_atomic({"new": {"role": "viewer"}})

        assert fake.read_text(encoding="utf-8") == original_content

    def test_temp_file_mode_0600_before_replace(self, tmp_path, monkeypatch):
        """The temp file must be chmod'd to 0600 before os.replace."""
        fake = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake)
        chmod_calls = []

        original_chmod = os.chmod
        original_replace = os.replace

        def tracking_chmod(path, mode):
            chmod_calls.append((path, mode))
            return original_chmod(path, mode)

        def tracking_replace(src, dst):
            # At the point of replace, the temp file should already be 0600
            assert os.stat(src).st_mode & 0o777 == 0o600
            return original_replace(src, dst)

        monkeypatch.setattr("os.chmod", tracking_chmod)
        monkeypatch.setattr("os.replace", tracking_replace)
        with _users_lock:
            save_users_atomic({"alice": {"role": "admin"}})

        # chmod should have been called with 0o600
        assert any(mode == 0o600 for _, mode in chmod_calls)


# ---------------------------------------------------------------------------
# Tests: mutate_users
# ---------------------------------------------------------------------------

class TestMutateUsers:
    def test_adds_user(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        def add(users):
            users["bob"] = {"role": "operator", "password_hash": "x", "salt": "y"}

        mutate_users(add)
        data = json.loads(fake.read_text(encoding="utf-8"))
        assert "bob" in data

    def test_removes_user(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"alice": {"role": "admin"}, "bob": {"role": "viewer"}}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        def remove(users):
            del users["alice"]

        mutate_users(remove)
        data = json.loads(fake.read_text(encoding="utf-8"))
        assert "alice" not in data
        assert "bob" in data

    def test_concurrent_mutations_no_lost_updates(self, tmp_path, monkeypatch):
        """Multiple threads adding different users must all succeed."""
        fake = tmp_path / "users.json"
        fake.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        n_threads = 10
        errors = []

        def worker(i):
            try:
                def add(users):
                    users[f"user_{i}"] = {"role": "viewer", "password_hash": "x", "salt": "y"}
                mutate_users(add)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        data = json.loads(fake.read_text(encoding="utf-8"))
        assert len(data) == n_threads
        for i in range(n_threads):
            assert f"user_{i}" in data


# ---------------------------------------------------------------------------
# Tests: authenticate_identity with disabled field
# ---------------------------------------------------------------------------

class TestAuthenticateIdentityDisabled:
    def test_disabled_user_rejected(self, tmp_path, monkeypatch):
        _make_user_in_file("alice", "correct-password", role="admin", disabled=True)
        with pytest.raises(ValueError):
            authenticate_identity(HTTPBasicCredentials(username="alice", password="correct-password"))

    def test_enabled_user_accepted(self, tmp_path, monkeypatch):
        _make_user_in_file("alice", "correct-password", role="admin", disabled=False)
        username, role = authenticate_identity(
            HTTPBasicCredentials(username="alice", password="correct-password")
        )
        assert username == "alice"
        assert role == "admin"

    def test_missing_disabled_treated_as_enabled(self, tmp_path, monkeypatch):
        """A record without a 'disabled' key should be treated as enabled."""
        record = create_password_record("correct-password", "operator")
        # Ensure 'disabled' key is absent
        assert "disabled" not in record
        users = {"alice": record}
        with _users_lock:
            save_users_atomic(users)
        username, role = authenticate_identity(
            HTTPBasicCredentials(username="alice", password="correct-password")
        )
        assert username == "alice"
        assert role == "operator"

    def test_wrong_password_rejected(self, tmp_path, monkeypatch):
        _make_user_in_file("alice", "correct-password", role="admin")
        with pytest.raises(ValueError):
            authenticate_identity(HTTPBasicCredentials(username="alice", password="wrong-password"))

    def test_unknown_user_rejected(self, tmp_path, monkeypatch):
        _make_user_in_file("alice", "correct-password", role="admin")
        with pytest.raises(ValueError):
            authenticate_identity(HTTPBasicCredentials(username="bob", password="correct-password"))

    def test_legacy_fallback_still_works(self, tmp_path, monkeypatch):
        """Legacy plain-text password file fallback must still work."""
        import app as app_module
        fake_users = tmp_path / "users.json"
        # Don't create the users file - it shouldn't exist
        monkeypatch.setattr("app.USERS_FILE", fake_users)

        fake_pw = tmp_path / "passwords.json"
        fake_pw.write_text("legacy-secret-pw", encoding="utf-8")
        monkeypatch.setattr("app.PASSWORD_FILE", fake_pw)
        monkeypatch.setattr("app.USERNAME", "admin")

        username, role = authenticate_identity(
            HTTPBasicCredentials(username="admin", password="legacy-secret-pw")
        )
        assert username == "admin"
        assert role == "admin"

    def test_legacy_fallback_wrong_password_rejected(self, tmp_path, monkeypatch):
        """Legacy plain-text password file with wrong password must be rejected."""
        fake_users = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake_users)

        fake_pw = tmp_path / "passwords.json"
        fake_pw.write_text("legacy-secret-pw", encoding="utf-8")
        monkeypatch.setattr("app.PASSWORD_FILE", fake_pw)
        monkeypatch.setattr("app.USERNAME", "admin")

        with pytest.raises(ValueError):
            authenticate_identity(
                HTTPBasicCredentials(username="admin", password="wrong-pw")
            )


# ---------------------------------------------------------------------------
# Tests: revoke_user_sessions
# ---------------------------------------------------------------------------

class TestRevokeUserSessions:
    def test_revokes_sessions_for_target_user(self):
        sid_alice = _create_session("alice", "admin")
        sid_bob = _create_session("bob", "viewer")

        assert _validate_session(sid_alice) is not None
        assert _validate_session(sid_bob) is not None

        revoke_user_sessions("alice")

        assert _validate_session(sid_alice) is None
        assert _validate_session(sid_bob) is not None

    def test_revokes_multiple_sessions_for_same_user(self):
        """Directly insert multiple sessions for the same user, then revoke all."""
        import secrets
        import time as _time
        from app import _hash_session_id

        # Create two sessions directly (bypassing rotation)
        sid1 = secrets.token_bytes(32).hex()
        sid2 = secrets.token_bytes(32).hex()
        now_mono = _time.monotonic()
        now_wall = _time.time()
        with _session_lock:
            _sessions[_hash_session_id(sid1)] = {
                "session_hash": _hash_session_id(sid1),
                "username": "alice",
                "role": "admin",
                "csrf_token": secrets.token_hex(32),
                "created_at": now_mono,
                "last_seen": now_mono,
                "wall_created": now_wall,
            }
            _sessions[_hash_session_id(sid2)] = {
                "session_hash": _hash_session_id(sid2),
                "username": "alice",
                "role": "admin",
                "csrf_token": secrets.token_hex(32),
                "created_at": now_mono,
                "last_seen": now_mono,
                "wall_created": now_wall,
            }

        assert _validate_session(sid1) is not None
        assert _validate_session(sid2) is not None

        revoke_user_sessions("alice")

        assert _validate_session(sid1) is None
        assert _validate_session(sid2) is None

    def test_no_error_for_user_with_no_sessions(self):
        revoke_user_sessions("nonexistent")

    def test_does_not_affect_other_users(self):
        sid_alice = _create_session("alice", "admin")
        sid_bob = _create_session("bob", "operator")
        sid_carol = _create_session("carol", "viewer")

        revoke_user_sessions("bob")

        assert _validate_session(sid_alice) is not None
        assert _validate_session(sid_bob) is None
        assert _validate_session(sid_carol) is not None


# ---------------------------------------------------------------------------
# Tests: concurrent mutation safety
# ---------------------------------------------------------------------------

class TestConcurrentMutationSafety:
    def test_concurrent_mutate_users_no_corruption(self, tmp_path, monkeypatch):
        """Concurrent mutations must not produce corrupted JSON."""
        fake = tmp_path / "users.json"
        fake.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        n = 20
        errors = []

        def worker(i):
            try:
                def add(users):
                    users[f"user_{i}"] = {"role": "viewer", "password_hash": "x", "salt": "y"}
                mutate_users(add)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        data = json.loads(fake.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert len(data) == n

    def test_concurrent_read_during_write(self, tmp_path, monkeypatch):
        """Reads during writes must not see torn/corrupt data."""
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"initial": {"role": "admin"}}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)

        stop = threading.Event()
        read_errors = []

        def reader():
            while not stop.is_set():
                try:
                    result = load_users()
                    assert isinstance(result, dict)
                    for v in result.values():
                        assert isinstance(v, dict)
                        assert "role" in v
                except Exception as e:
                    read_errors.append(e)

        def writer():
            for i in range(10):
                def add(users):
                    users[f"w_{i}"] = {"role": "viewer", "password_hash": "x", "salt": "y"}
                mutate_users(add)

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        writer_thread.join()
        stop.set()
        reader_thread.join()

        assert read_errors == []


# ---------------------------------------------------------------------------
# Tests: strict loader (malformed-file safety for admin mutations)
# ---------------------------------------------------------------------------

class TestStrictLoader:
    """load_users_unlocked_strict must distinguish a missing file from a
    malformed one so admin mutations fail safely without overwriting."""

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        monkeypatch.setattr("app.USERS_FILE", fake)
        assert not fake.exists()
        assert load_users_unlocked_strict() == {}

    def test_well_formed_file_returns_records(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(
            json.dumps(
                {
                    "alice": {"role": "admin", "salt": "s", "password_hash": "h"},
                    "bob": {"role": "viewer", "salt": "s", "password_hash": "h"},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("app.USERS_FILE", fake)
        result = load_users_unlocked_strict()
        assert set(result) == {"alice", "bob"}
        assert result["alice"]["role"] == "admin"

    def test_invalid_json_raises(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text("{ this is not valid json", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        with pytest.raises(ValueError):
            load_users_unlocked_strict()

    def test_non_dict_top_level_raises(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps([{"role": "admin"}]), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        with pytest.raises(ValueError):
            load_users_unlocked_strict()

    def test_non_dict_record_raises(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"alice": "not-a-dict"}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        with pytest.raises(ValueError):
            load_users_unlocked_strict()

    def test_invalid_role_record_raises(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"alice": {"role": "superuser"}}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        with pytest.raises(ValueError):
            load_users_unlocked_strict()

    def test_unreadable_file_raises(self, tmp_path, monkeypatch):
        fake = tmp_path / "users.json"
        fake.write_text(json.dumps({"alice": {"role": "admin"}}), encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        fake.chmod(0o000)
        try:
            with pytest.raises(ValueError):
                load_users_unlocked_strict()
        finally:
            fake.chmod(0o644)

    def test_strict_vs_lenient_divergence_on_malformed(self, tmp_path, monkeypatch):
        """The lenient loader returns {} on malformed; the strict loader raises.
        This divergence is what lets admin mutations fail safely."""
        fake = tmp_path / "users.json"
        fake.write_text("{ broken", encoding="utf-8")
        monkeypatch.setattr("app.USERS_FILE", fake)
        # Lenient: silently empty (legacy behavior preserved).
        assert load_users_unlocked() == {}
        # Strict: raises so a mutation can abort without overwriting.
        with pytest.raises(ValueError):
            load_users_unlocked_strict()
