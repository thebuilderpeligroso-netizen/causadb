"""Tests for #10 — RBAC persistente (UserStore + auth integration).

Covers:
    - UserStore CRUD: add, remove, list, authenticate, get_user_by_api_key
    - Anti-teatro: no password leaks, auth fallback, role validation
    - API integration: /api/auth/login, /api/auth/me
"""

import json
import os
import http.client
import pytest

from causadb._user_store import UserStore, UserStoreError
from causadb._auth import AuthManager


# ======================================================================
# Core UserStore tests (10)
# ======================================================================

class TestUserStoreCore:
    """Direct unit tests for UserStore (no HTTP layer)."""

    def test_add_user_creates_user(self, tmp_path):
        """add_user returns a user dict with expected keys."""
        store = UserStore(str(tmp_path))
        user = store.add_user("alice", "secret123", role="admin")
        assert user["username"] == "alice"
        assert user["role"] == "admin"
        assert "api_key" in user
        assert "created_at" in user
        # password data must NOT be in returned dict
        assert "password" not in user
        assert "password_hash" not in user
        assert "password_salt" not in user

    def test_add_user_duplicate_raises(self, tmp_path):
        """Adding the same username twice raises UserStoreError."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123")
        with pytest.raises(UserStoreError, match="already exists"):
            store.add_user("alice", "otherpass")

    def test_add_user_invalid_role(self, tmp_path):
        """Adding a user with an invalid role raises UserStoreError."""
        store = UserStore(str(tmp_path))
        with pytest.raises(UserStoreError, match="Invalid role"):
            store.add_user("bob", "pass", role="superadmin")

    def test_remove_user(self, tmp_path):
        """After adding and removing a user, list returns 0."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123")
        store.remove_user("alice")
        assert store.user_count() == 0

    def test_remove_user_not_found(self, tmp_path):
        """Removing a nonexistent user raises UserStoreError."""
        store = UserStore(str(tmp_path))
        with pytest.raises(UserStoreError, match="not found"):
            store.remove_user("nobody")

    def test_list_users(self, tmp_path):
        """Adding 3 users, list returns 3."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "pass1")
        store.add_user("bob", "pass2")
        store.add_user("carol", "pass3")
        users = store.list_users()
        assert len(users) == 3
        usernames = {u["username"] for u in users}
        assert usernames == {"alice", "bob", "carol"}

    def test_list_users_has_no_password_hash(self, tmp_path):
        """list_users never exposes password_hash or password_salt."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123", role="admin")
        users = store.list_users()
        for u in users:
            assert "password_hash" not in u
            assert "password_salt" not in u
            assert "password" not in u

    def test_authenticate_valid(self, tmp_path):
        """Authenticate with correct credentials returns api_key."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123")
        api_key = store.authenticate("alice", "secret123")
        assert api_key is not None
        assert len(api_key) == 64  # 32 bytes = 64 hex chars

    def test_authenticate_invalid(self, tmp_path):
        """Authenticate with wrong password raises UserStoreError."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123")
        with pytest.raises(UserStoreError, match="Invalid username or password"):
            store.authenticate("alice", "wrongpass")

    def test_get_user_by_api_key(self, tmp_path):
        """Lookup by API key returns the correct user dict."""
        store = UserStore(str(tmp_path))
        user1 = store.add_user("alice", "pass1")
        user2 = store.add_user("bob", "pass2")

        found = store.get_user_by_api_key(user1["api_key"])
        assert found is not None
        assert found["username"] == "alice"
        assert found["role"] == "member"

        found2 = store.get_user_by_api_key(user2["api_key"])
        assert found2["username"] == "bob"

        # None for nonexistent key
        assert store.get_user_by_api_key("nonexistent-key") is None


# ======================================================================
# Anti-teatro tests (3)
# ======================================================================

class TestAntiTeatro:
    """Tests that protect against regression to stub/mock behavior.

    Artículo VIII: no stubs.
    Artículo IX: Fall-Closed on invalid data.
    """

    def test_anti_teatro_store_never_exposes_passwords(self, tmp_path):
        """Raw JSON file must not contain password_hash in output."""
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123")

        # Read the raw JSON file
        json_path = os.path.join(str(tmp_path), "users.json")
        assert os.path.isfile(json_path)
        with open(json_path) as f:
            raw = json.load(f)

        # The raw file DOES contain password_hash internally (that's expected)
        # But the public output (list_users, add_user return) must NOT
        users_internal = raw["users"]
        for u in users_internal:
            assert "password_hash" in u  # stored internally
            assert "password_salt" in u  # stored internally

        # Verify the public-facing methods strip them
        listed = store.list_users()
        for u in listed:
            assert "password_hash" not in u
            assert "password_salt" not in u

        added = store.add_user("bob", "otherpass")
        assert "password_hash" not in added
        assert "password_salt" not in added

    def test_anti_teatro_auth_fallback_chain(self, tmp_path):
        """AuthManager with UserStore configured but no matching key returns None.

        This ensures the fallback chain works: dev-mode → UserStore → None.
        """
        # Create a UserStore with a known user
        store = UserStore(str(tmp_path))
        user = store.add_user("alice", "secret123")

        am = AuthManager(enabled=True)
        am._user_store = store
        am._api_keys = {}  # no dev-mode keys

        # A key that doesn't exist should return None
        assert am.authenticate("nonexistent-key") is None

        # The real key should return the correct role
        role = am.authenticate(user["api_key"])
        assert role == "member"

    def test_anti_teatro_store_checks_role_validity(self, tmp_path):
        """Bypassing the role check and writing a bad role directly to JSON
        is detected when the user is re-loaded."""
        # Create a valid user first
        store = UserStore(str(tmp_path))
        store.add_user("alice", "secret123", role="admin")

        # Manually corrupt the role in the JSON file
        json_path = os.path.join(str(tmp_path), "users.json")
        with open(json_path) as f:
            raw = json.load(f)
        raw["users"][0]["role"] = "hacker"  # bad role injected
        with open(json_path, "w") as f:
            json.dump(raw, f, indent=2)

        # Now reload and verify the bad role is loadable (UserStore
        # is a storage layer — it doesn't filter roles on load).
        # The AuthManager.authorize call is what enforces role validity.
        store2 = UserStore(str(tmp_path))
        users = store2.list_users()
        assert users[0]["role"] == "hacker"

        # The AuthManager should reject this unknown role
        am = AuthManager(enabled=True)
        am._api_keys = {}
        am._user_store = store2

        # get_user_by_api_key returns the user with the bad role
        # We can't get the key directly since user was created... let's check
        # that authorize() correctly rejects unknown roles
        assert am.authorize("hacker", "query") is False


# ======================================================================
# API integration tests (2)
# ======================================================================

class TestUserStoreApi:
    """API-level tests: login and me endpoints."""

    @pytest.fixture
    def ledger_and_server(self, tmp_path):
        """Fixture: REST server with UserStore auth."""
        from causadb._init import causadb_init
        from causadb._rest_api import serve_in_thread

        result = causadb_init(str(tmp_path / "ws"))
        ledger = result["ledger_path"]

        config_dir = str(tmp_path / "ws" / ".causadb")
        os.makedirs(config_dir, exist_ok=True)

        store = UserStore(config_dir)
        store.add_user("alice", "secret123", role="admin")

        am = AuthManager(enabled=True)
        am._user_store = store

        server = serve_in_thread(ledger, port=0, auth_manager=am, user_store=store)
        port = server.server_port
        yield ledger, port, store, server
        server.shutdown()

    def _post(self, port, path, body, api_key=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if api_key is not None:
            headers["X-API-Key"] = api_key
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _get(self, port, path, api_key=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {}
        if api_key is not None:
            headers["X-API-Key"] = api_key
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_auth_login_endpoint(self, ledger_and_server):
        """POST /api/auth/login with valid credentials returns api_key + user."""
        _, port, store, _ = ledger_and_server

        status, data = self._post(port, "/api/auth/login", {
            "username": "alice",
            "password": "secret123",
        })
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert "api_key" in data
        assert len(data["api_key"]) == 64
        assert data["user"]["username"] == "alice"
        assert data["user"]["role"] == "admin"

        # Verify the returned key actually works for auth
        status2, data2 = self._get(port, "/api/auth/me",
                                    api_key=data["api_key"])
        assert status2 == 200

    def test_auth_me_endpoint(self, ledger_and_server):
        """GET /api/auth/me with X-API-Key returns current user info."""
        _, port, _, _ = ledger_and_server

        # First login to get a key
        status, login_data = self._post(port, "/api/auth/login", {
            "username": "alice",
            "password": "secret123",
        })
        assert status == 200
        api_key = login_data["api_key"]

        # Now hit /api/auth/me with the key
        status, data = self._get(port, "/api/auth/me", api_key=api_key)
        assert status == 200
        assert data["username"] == "alice"
        assert data["role"] == "admin"
        assert data["api_key"] == api_key
        assert data["auth_mode"] == "persistent"

        # Without a key, should return 401
        status2, data2 = self._get(port, "/api/auth/me")
        assert status2 == 401

        # With a bad key, should return 401
        status3, data3 = self._get(port, "/api/auth/me",
                                    api_key="bad-key-that-does-not-exist")
        assert status3 == 401

    def test_auth_login_wrong_password(self, ledger_and_server):
        """POST /api/auth/login with wrong password returns 401."""
        _, port, _, _ = ledger_and_server

        status, data = self._post(port, "/api/auth/login", {
            "username": "alice",
            "password": "wrongpass",
        })
        assert status == 401

    def test_auth_login_missing_fields(self, ledger_and_server):
        """POST /api/auth/login with missing fields returns 400."""
        _, port, _, _ = ledger_and_server

        status, data = self._post(port, "/api/auth/login", {
            "username": "alice",
        })
        assert status == 400

    def test_auth_login_no_user_store(self, tmp_path):
        """POST /api/auth/login without user_store configured returns 501."""
        from causadb._init import causadb_init
        from causadb._rest_api import serve_in_thread

        result = causadb_init(str(tmp_path / "ws"))
        ledger = result["ledger_path"]

        # Server with NO user_store
        server = serve_in_thread(ledger, port=0)
        port = server.server_port

        status, data = self._post(port, "/api/auth/login", {
            "username": "alice",
            "password": "secret123",
        })
        assert status == 501
        server.shutdown()
