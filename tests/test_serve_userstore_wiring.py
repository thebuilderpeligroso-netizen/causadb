"""Wiring serve → UserStore (RBAC persistente en producción).

El serve de producción (systemd) debe enchufar el UserStore del workspace
para que las llaves de usuarios persistentes autentiquen con su rol, con
dev-keys-primero (maestra admin preservada) y degradación a solo-maestra
si el store está vacío/ausente.
"""

import http.client
import json
import os

MASTER_KEY = "maestra-larga-de-prueba-1234567890"
AUDITOR_USER = "audit1"
AUDITOR_PASS = "clave-auditor-123"


def _make_workspace_with_auditor(tmp_path):
    from causadb._init import causadb_init
    from causadb._user_store import UserStore

    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    config_dir = os.path.dirname(os.path.abspath(ledger))
    store = UserStore(config_dir)
    auditor = store.add_user(AUDITOR_USER, AUDITOR_PASS, role="auditor")
    return ledger, config_dir, auditor


def _wired_auth_and_server(ledger):
    """Cablea AuthManager vía helper NUEVO de _cmd_serve y levanta server."""
    from causadb._auth import AuthManager
    from causadb.cli import _cmd_serve
    from causadb._rest_api import serve_in_thread

    assert hasattr(_cmd_serve, "_attach_user_store"), (
        "falta helper _attach_user_store en causadb/cli/_cmd_serve.py"
    )
    am = AuthManager()
    am.enable({MASTER_KEY: "admin"})
    _cmd_serve._attach_user_store(am, ledger)
    assert am.user_store is not None, "el helper debe adjuntar user_store"
    server = serve_in_thread(
        ledger, port=0, auth_manager=am, user_store=am.user_store
    )
    return am, server


def _get(port, path, api_key=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    conn.close()
    return resp.status, data


def _post(port, path, body, api_key=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    conn.request("POST", path, json.dumps(body), headers)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    conn.close()
    return resp.status, data


def test_auditor_key_can_query(tmp_path):
    """(a) llave auditor persistente → GET /api/query 200."""
    ledger, _, auditor = _make_workspace_with_auditor(tmp_path)
    _, server = _wired_auth_and_server(ledger)
    try:
        port = server.server_port
        status, _ = _get(port, "/api/query", api_key=auditor["api_key"])
        assert status == 200, f"auditor debe poder query, fue {status}"
    finally:
        server.shutdown()


def test_auditor_key_cannot_log(tmp_path):
    """(b) llave auditor persistente → POST /api/log denegado (403/401)."""
    ledger, _, auditor = _make_workspace_with_auditor(tmp_path)
    _, server = _wired_auth_and_server(ledger)
    try:
        port = server.server_port
        status, _ = _post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "t",
            "source": "test",
            "payload": {"path": "x.txt"},
        }, api_key=auditor["api_key"])
        assert status in (401, 403), f"auditor no debe loguear, fue {status}"
        assert status == 403, f"auditor autenticado sin permiso debe ser 403, fue {status}"
    finally:
        server.shutdown()


def test_master_key_stays_admin(tmp_path):
    """(c) llave maestra sigue admin (puede loguear)."""
    ledger, _, _ = _make_workspace_with_auditor(tmp_path)
    _, server = _wired_auth_and_server(ledger)
    try:
        port = server.server_port
        status, data = _post(port, "/api/log", {
            "event_type": "FILE_MODIFIED",
            "ctx_id": "t",
            "source": "test",
            "payload": {"path": "x.txt"},
        }, api_key=MASTER_KEY)
        assert status == 200, f"maestra debe seguir admin, fue {status}: {data}"
        assert "event_id" in data
    finally:
        server.shutdown()


def test_login_then_me_as_auditor(tmp_path):
    """(d) login usuario/clave → api_key usable en /api/auth/me con role auditor."""
    ledger, _, _ = _make_workspace_with_auditor(tmp_path)
    _, server = _wired_auth_and_server(ledger)
    try:
        port = server.server_port
        status, data = _post(port, "/api/auth/login", {
            "username": AUDITOR_USER, "password": AUDITOR_PASS,
        })
        assert status == 200, f"login debe ser 200, fue {status}: {data}"
        api_key = data["api_key"]
        status2, me = _get(port, "/api/auth/me", api_key=api_key)
        assert status2 == 200, f"/api/auth/me debe ser 200, fue {status2}: {me}"
        assert me["role"] == "auditor"
        assert me["username"] == AUDITOR_USER
    finally:
        server.shutdown()


def test_attach_user_store_helper_direct(tmp_path):
    """(e) ANTI-TEATRO: ejercita el helper NUEVO sin HTTP.

    Construye AuthManager+store desde workspace temporal con 1 auditor y
    aserta authenticate(api_key)=="auditor" y authenticate(master)=="admin".
    """
    from causadb._auth import AuthManager
    from causadb.cli import _cmd_serve

    assert hasattr(_cmd_serve, "_attach_user_store"), (
        "falta helper _attach_user_store en causadb/cli/_cmd_serve.py"
    )
    ledger, _, auditor = _make_workspace_with_auditor(tmp_path)
    am = AuthManager()
    am.enable({MASTER_KEY: "admin"})
    _cmd_serve._attach_user_store(am, ledger)
    assert am.authenticate(auditor["api_key"]) == "auditor"
    assert am.authenticate(MASTER_KEY) == "admin"
    assert am.authenticate("clave-inexistente-xyz") is None


def test_attach_user_store_empty_degrades_to_master_only(tmp_path, caplog):
    """Store vacío/ausente → degrada a solo-maestra, serve seguiría arrancando."""
    import logging
    from causadb._init import causadb_init
    from causadb._auth import AuthManager
    from causadb.cli import _cmd_serve

    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    am = AuthManager()
    am.enable({MASTER_KEY: "admin"})
    with caplog.at_level(logging.INFO):
        _cmd_serve._attach_user_store(am, ledger)
    assert am.authenticate(MASTER_KEY) == "admin"
    assert am.authenticate("cualquier-clave-xyz") is None
