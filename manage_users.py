#!/usr/bin/env python3
"""Provision dashboard users without storing plaintext passwords."""

import argparse
import getpass
import hashlib
import json
import os
import secrets
from pathlib import Path


DEFAULT_USERS_FILE = Path.home() / ".local/share/m1-agent/dashboard-data/users.json"
ROLES = ("viewer", "operator", "admin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Local AI dashboard users")
    parser.add_argument("username")
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--file", type=Path, default=DEFAULT_USERS_FILE)
    args = parser.parse_args()
    if not args.username or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in args.username):
        raise SystemExit("Username may contain letters, numbers, dots, underscores, and hyphens")

    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters")

    path = args.file.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    users = {}
    if path.is_file():
        users = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(users, dict):
            raise SystemExit("Users file must contain a JSON object")

    iterations = 600_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    users[args.username] = {
        "role": args.role,
        "salt": salt.hex(),
        "password_hash": digest.hex(),
        "iterations": iterations,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(users, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    print(f"Saved {args.username} with role {args.role} to {path}")


if __name__ == "__main__":
    main()
