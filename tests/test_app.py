from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

import app


def test_authentication_accepts_configured_admin(tmp_path: Path):
    password = tmp_path / "password"
    password.write_text("secret-value", encoding="utf-8")
    with patch.object(app, "PASSWORD_FILE", password):
        assert app.authenticate(HTTPBasicCredentials(username="admin", password="secret-value")) == "admin"


def test_authentication_rejects_wrong_password(tmp_path: Path):
    password = tmp_path / "password"
    password.write_text("secret-value", encoding="utf-8")
    with patch.object(app, "PASSWORD_FILE", password), pytest.raises(HTTPException) as exc:
        app.authenticate(HTTPBasicCredentials(username="admin", password="wrong"))
    assert exc.value.status_code == 401
