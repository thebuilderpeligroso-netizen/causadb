"""Tests for I.2 — Roles / Auth system.

Covers:
    - AuthManager unit behavior (authenticate, authorize, enable validation)
    - REST API auth integration (401/403 responses)
    - Role-based access matrix:
        admin  → log, query, register_type, config, replay
        member → log, query, replay (no register_type, no config)
        auditor→ query only
"""

import json
import http.client
import pytest

from causadb._auth import AuthManager


# ---------------------------------------------------------------------------
# AuthManager unit tests
# ---------------------------------------------------------------------------

class TestAuthManager:
    """Direct unit tests for AuthManager (no HTTP layer)."""

    def test_default_disabled(self):
        """Por defecto sin auth — todas las operaciones permitidas."""
        am = AuthManager()
        assert am.enabled is False
        # authenticate debe retornar un rol aunque no haya keys
        assert am.authenticate(None) == "admin"
        assert am.authenticate("some-key") == "admin"
        # authorize siempre True
        assert am.authorize("anyone", "log") is True
        assert am.authorize("anyone", "config") is True

    def test_enable_activates_auth(self):
        """enable() activa auth y registra keys."""
        am = AuthManager()
        am.enable({"admin-key-12345": "admin"})
        assert am.enabled is True
        assert am.authenticate("admin-key-12345") == "admin"
        assert am.authenticate("wrong-key") is None

    def test_enable_rejects_short_key(self):
        """API key debe tener mínimo 8 caracteres."""
        am = AuthManager()
        with pytest.raises(ValueError, match="at least 8 characters"):
            am.enable({"short": "admin"})

    def test_enable_rejects_invalid_role(self):
        """Rol inválido levanta ValueError."""
        am = AuthManager()
        with pytest.raises(ValueError, match="Invalid role"):
            am.enable({"valid-key-12345": "superadmin"})

    def test_authenticate_without_key_when_enabled(self):
        """Con auth activo y sin API key → None."""
        am = AuthManager()
        am.enable({"admin-key-12345": "admin"})
        assert am.authenticate(None) is None
        assert am.authenticate("") is None

    def test_authorize_matrix_admin(self):
        """Admin puede hacer todo."""
        am = AuthManager()
        am.enable({"admin-key-12345": "admin"})
        assert am.authorize("admin", "log") is True
        assert am.authorize("admin", "query") is True
        assert am.authorize("admin", "register_type") is True
        assert am.authorize("admin", "config") is True
        assert am.authorize("admin", "replay") is True

    def test_authorize_matrix_member(self):
        """Member puede log, query, replay; no register_type ni config."""
        am = AuthManager()
        am.enable({"member-key-123": "member"})
        assert am.authorize("member", "log") is True
        assert am.authorize("member", "query") is True
        assert am.authorize("member", "replay") is True
        assert am.authorize("member", "register_type") is False
        assert am.authorize("member", "config") is False

    def test_authorize_matrix_auditor(self):
        """Auditor solo puede query."""
        am = AuthManager()
        am.enable({"auditor-key-12": "auditor"})
        assert am.authorize("auditor", "query") is True
        assert am.authorize("auditor", "log") is False
        assert am.authorize("auditor", "register_type") is False
        assert am.authorize("auditor", "config") is False
        assert am.authorize("auditor", "replay") is False

    def test_authorize_unknown_action(self):
        """Acción desconocida → False."""
        am = AuthManager()
        am.enable({"admin-key-12345": "admin"})
        assert am.authorize("admin", "nonexistent") is False

    def test_authorize_unknown_role(self):
        """Rol desconocido → False."""
        am = AuthManager()
        am.enable({"admin-key-12345": "admin"})
        assert am.authorize("hacker", "query") is False


# ---------------------------------------------------------------------------
# REST API integration tests (auth enabled)
# ---------------------------------------------------------------------------

class TestRestAuth:
    """Auth integration via HTTP — auth-enabled server."""

    @pytest.fixture
    def ledger_and_server_with_auth(self, tmp_path):
        """Fixture: servidor REST con auth habilitado y 3 API keys."""
        from causadb._init import causadb_init
        from causadb._rest_api import serve_in_thread

        result = causadb_init(str(tmp_path / "ws"))
        ledger = result["ledger_path"]

        am = AuthManager()
        am.enable({
            "admin-key-12345": "admin",
            "member-key-123": "member",
            "auditor-key-12": "auditor",
        })
        server = serve_in_thread(ledger, port=0, auth_manager=am)
        port = server.server_port
        yield ledger, port, server
        server.shutdown()

    @pytest.fixture
    def ledger_and_server_default(self, tmp_path):
        """Fixture: servidor REST sin auth (default)."""
        from causadb._init import causadb_init
        from causadb._rest_api import serve_in_thread

        result = causadb_init(str(tmp_path / "ws"))
        ledger = result["ledger_path"]
        server = serve_in_thread(ledger, port=0)
        port = server.server_port
        yield ledger, port, server
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

    # -- Default (no auth) behaves same as before --

    def test_default_no_auth_works(self, ledger_and_server_default):
        """Sin auth, todas las operaciones funcionan sin API key."""
        _, port, _ = ledger_and_server_default
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        })
        assert status == 200
        assert "event_id" in data

    def test_default_auth_header_ignored(self, ledger_and_server_default):
        """Sin auth, el header X-API-Key se ignora."""
        _, port, _ = ledger_and_server_default
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        }, api_key="some-random-key")
        assert status == 200

    # -- Auth enabled: 401 / 403 tests --

    def test_no_key_returns_401(self, ledger_and_server_with_auth):
        """Sin API key → 401."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        })
        assert status == 401
        assert "unauthorized" in data.get("error", "")

    def test_wrong_key_returns_401(self, ledger_and_server_with_auth):
        """API key inválida → 401."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        }, api_key="nonexistent-key")
        assert status == 401

    # -- Role matrix: register_type (admin only) --

    def test_admin_can_register_types(self, ledger_and_server_with_auth):
        """admin puede POST a /api/register-type (simulado)."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/register-type", {
            "name": "MY_CUSTOM_EVENT",
            "required_fields": ["id"],
        }, api_key="admin-key-12345")
        assert status == 200
        assert data.get("status") == "ok"
        assert data.get("registered") == "MY_CUSTOM_EVENT"

    def test_member_cannot_register_types(self, ledger_and_server_with_auth):
        """member recibe 403 al intentar register-type."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/register-type", {
            "name": "MY_CUSTOM_EVENT",
            "required_fields": ["id"],
        }, api_key="member-key-123")
        assert status == 403
        assert "forbidden" in data.get("error", "")

    def test_auditor_cannot_register_types(self, ledger_and_server_with_auth):
        """auditor recibe 403 al intentar register-type."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/register-type", {
            "name": "MY_CUSTOM_EVENT",
            "required_fields": ["id"],
        }, api_key="auditor-key-12")
        assert status == 403

    # -- Role matrix: log (admin + member) --

    def test_admin_can_log(self, ledger_and_server_with_auth):
        """admin puede loguear eventos."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        }, api_key="admin-key-12345")
        assert status == 200
        assert "event_id" in data

    def test_member_can_log(self, ledger_and_server_with_auth):
        """member puede loguear eventos."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        }, api_key="member-key-123")
        assert status == 200
        assert "event_id" in data

    # -- Role matrix: query (all roles) --

    def test_auditor_read_only(self, ledger_and_server_with_auth):
        """auditor puede query, no puede log."""
        _, port, _ = ledger_and_server_with_auth
        # Query GET debe funcionar
        status, data = self._get(port, "/api/events", api_key="auditor-key-12")
        assert status == 200
        assert isinstance(data, list)
        # Log POST debe fallar
        status, data = self._post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "test",
            "source": "auth:test",
            "payload": {"path": "test.txt"},
        }, api_key="auditor-key-12")
        assert status == 403

    def test_auditor_can_query_post(self, ledger_and_server_with_auth):
        """auditor puede hacer POST /api/query."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/query", {
            "event_type": "FILE_MODIFIED",
        }, api_key="auditor-key-12")
        assert status == 200

    def test_member_can_query(self, ledger_and_server_with_auth):
        """member puede hacer GET /api/query."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._get(port, "/api/events", api_key="member-key-123")
        assert status == 200

    def test_admin_can_query(self, ledger_and_server_with_auth):
        """admin puede hacer query."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._get(port, "/api/events", api_key="admin-key-12345")
        assert status == 200

    # -- Role matrix: replay (admin + member) --

    def test_admin_can_replay(self, ledger_and_server_with_auth):
        """admin puede hacer replay."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/replay", {},
                                  api_key="admin-key-12345")
        assert status == 200

    def test_member_can_replay(self, ledger_and_server_with_auth):
        """member puede hacer replay."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/replay", {},
                                  api_key="member-key-123")
        assert status == 200

    def test_auditor_cannot_replay(self, ledger_and_server_with_auth):
        """auditor NO puede hacer replay."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._post(port, "/api/replay", {},
                                  api_key="auditor-key-12")
        assert status == 403

    # -- Dashboard is always public --

    def test_dashboard_public_no_key(self, ledger_and_server_with_auth):
        """Dashboard no requiere auth — se sirve aunque no haya key."""
        _, port, _ = ledger_and_server_with_auth
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/dashboard/")
        resp = conn.getresponse()
        conn.close()
        # 200 o 404 (si no hay archivos dashboard) — pero nunca 401
        assert resp.status in (200, 404)

    def test_health_public_no_key(self, ledger_and_server_with_auth):
        """Health endpoint es público — no requiere auth."""
        _, port, _ = ledger_and_server_with_auth
        status, data = self._get(port, "/api/health")
        assert status == 200
        assert data.get("status") == "ok"
