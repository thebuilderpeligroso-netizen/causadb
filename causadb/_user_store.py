"""Persistent RBAC user store. 0 deps, stdlib only.

Stores users in ``.causadb/users.json`` with pbkdf2_hmac password hashing.
Thread-safe. All file writes use atomic replace via ``os.replace``.
"""

import hashlib
import json
import os
import secrets
import threading
from typing import Optional

STORE_FILENAME = "users.json"
SUPPORTED_ROLES = ("admin", "member", "auditor")


class UserStoreError(Exception):
    """Base error for user store operations."""


class UserStore:
    """Thread-safe user store backed by JSON file.

    Args:
        config_dir: Absolute path to the ``.causadb/`` directory (or any
            directory that should contain ``users.json``).
    """

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self._path = os.path.join(config_dir, STORE_FILENAME)
        self._lock = threading.Lock()

    # ── private helpers ─────────────────────────────────────────────

    def _load(self) -> dict:
        if not os.path.isfile(self._path):
            return {"users": [], "version": 1}
        with open(self._path) as f:
            return json.load(f)

    def _save(self, data: dict):
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    @staticmethod
    def _user_to_public(user: dict) -> dict:
        return {
            "username": user["username"],
            "role": user["role"],
            "api_key": user["api_key"],
            "created_at": user["created_at"],
        }

    # ── public API ──────────────────────────────────────────────────

    def add_user(self, username: str, password: str, role: str = "member") -> dict:
        """Add a new user.

        Args:
            username: Unique username.
            password: Cleartext password (hashed with pbkdf2_hmac before
                storage).
            role: One of ``admin``, ``member``, ``auditor``.

        Returns:
            Public user dict (without password data).

        Raises:
            UserStoreError: If the username already exists or the role is
                invalid.
        """
        if role not in SUPPORTED_ROLES:
            raise UserStoreError(
                f"Invalid role: '{role}'. Must be one of {SUPPORTED_ROLES}"
            )

        with self._lock:
            data = self._load()
            for u in data["users"]:
                if u["username"] == username:
                    raise UserStoreError(f"User '{username}' already exists")

            salt = os.urandom(16)
            pw_hash = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), salt, 100000
            )
            user = {
                "username": username,
                "password_salt": salt.hex(),
                "password_hash": pw_hash.hex(),
                "role": role,
                "api_key": secrets.token_hex(32),
                "created_at": "REPLACED_AT_RUNTIME",
            }
            import datetime
            user["created_at"] = datetime.datetime.utcnow().isoformat() + "Z"

            data["users"].append(user)
            self._save(data)
            return self._user_to_public(user)

    def remove_user(self, username: str):
        """Remove a user by username.

        Raises:
            UserStoreError: If the user does not exist.
        """
        with self._lock:
            data = self._load()
            idx = None
            for i, u in enumerate(data["users"]):
                if u["username"] == username:
                    idx = i
                    break
            if idx is None:
                raise UserStoreError(f"User '{username}' not found")
            data["users"].pop(idx)
            self._save(data)

    def list_users(self) -> list:
        """Return a list of all users (public dicts, no password data)."""
        with self._lock:
            data = self._load()
            return [self._user_to_public(u) for u in data["users"]]

    def authenticate(self, username: str, password: str) -> str:
        """Authenticate a user by username and password.

        Returns:
            The user's ``api_key`` on success.

        Raises:
            UserStoreError: If credentials are invalid.
        """
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if u["username"] == username:
                    salt = bytes.fromhex(u["password_salt"])
                    pw_hash = hashlib.pbkdf2_hmac(
                        "sha256", password.encode(), salt, 100000
                    )
                    if pw_hash.hex() == u["password_hash"]:
                        return u["api_key"]
            raise UserStoreError("Invalid username or password")

    def get_user_by_api_key(self, api_key: str) -> Optional[dict]:
        """Look up a user by API key.

        Returns:
            Public user dict, or ``None`` if not found.
        """
        with self._lock:
            data = self._load()
            for u in data["users"]:
                if u["api_key"] == api_key:
                    return self._user_to_public(u)
            return None

    def user_count(self) -> int:
        """Return the number of registered users."""
        with self._lock:
            return len(self._load()["users"])
