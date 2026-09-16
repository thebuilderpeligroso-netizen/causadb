"""Fase 2 "Puertas con traba" — tests RED primero (TDD estricto).

Alcance exacto del plan auditado:
  1. Helper compartido require_api_key (hmac.compare_digest, nunca loguea).
  2. MCP-HTTP: key en CADA request (401 sin/mal, 200 bien). stdio sin cambios.
  3. REST serve: sin auth NO arranca (falla rápido, mensaje con CAUSADB_API_KEY / archivo 0600).
  4. Webhook TradingView FASE 1: secreto en URL (404 si no matchea) + body estricto
     (400 + NADA al ledger si vacío/inválido). Rate-limit básico + IPs opcional.
"""
import hmac
import http.client
import json

import pytest


# ---------------------------------------------------------------------------
# 1. Helper compartido require_api_key
# ---------------------------------------------------------------------------

def test_require_api_key_uses_compare_digest():
    """require_api_key debe comparar con hmac.compare_digest (timing-safe)."""
    from causadb import _auth as auth_mod
    assert hasattr(auth_mod, "require_api_key"), "falta helper require_api_key en _auth.py"
    # Espía compare_digest para probar que SE USA (no == a medida).
    calls = []
    orig = hmac.compare_digest
    def spy(a, b):
        calls.append((a, b))
        return orig(a, b)
    import hmac as _hmac_mod
    _hmac_mod.compare_digest = spy
    try:
        assert auth_mod.require_api_key("abc12345", "abc12345") is True
        assert auth_mod.require_api_key("abc12345", "zzz99999") is False
    finally:
        _hmac_mod.compare_digest = orig
    assert len(calls) >= 2, "require_api_key debe usar hmac.compare_digest"


def test_require_api_key_rejects_empty_and_never_logs(caplog):
    """None/vacío → False; la key nunca va a logs."""
    import logging
    from causadb._auth import require_api_key
    with caplog.at_level(logging.DEBUG):
        assert require_api_key(None, "secret123") is False
        assert require_api_key("", "secret123") is False
        assert require_api_key("secret123", "") is False
        assert require_api_key("secret123", None) is False
    for rec in caplog.records:
        assert "secret123" not in rec.getMessage(), "la key nunca debe loguearse"


# ---------------------------------------------------------------------------
# 2. MCP-HTTP: key en CADA request
# ---------------------------------------------------------------------------

def _mcp_auth_headers(api_key=None, bearer=None):
    h = {}
    if api_key is not None:
        h["X-API-Key"] = api_key
    if bearer is not None:
        h["Authorization"] = f"Bearer {bearer}"
    return h


def test_mcp_http_sin_key_401():
    """Sin key → 401 con mensaje que dice cómo conseguirla (CAUSADB_MCP_API_KEY)."""
    from causadb.mcp.server import is_mcp_request_authorized, mcp_unauthorized_body
    assert is_mcp_request_authorized({}, "clave-real-123") is False
    body = mcp_unauthorized_body()
    assert "CAUSADB_MCP_API_KEY" in json.dumps(body), (
        "el 401 debe explicar cómo conseguir la key (env CAUSADB_MCP_API_KEY)"
    )


def test_mcp_http_key_mal_401():
    """Key incorrecta → 401 (no 200, no 403)."""
    from causadb.mcp.server import is_mcp_request_authorized
    assert is_mcp_request_authorized(_mcp_auth_headers(api_key="clave-mala"), "clave-real-123") is False
    assert is_mcp_request_authorized(_mcp_auth_headers(bearer="clave-mala"), "clave-real-123") is False


def test_mcp_http_key_bien_200():
    """Key correcta (X-API-Key o Bearer) → autorizado."""
    from causadb.mcp.server import is_mcp_request_authorized
    assert is_mcp_request_authorized(_mcp_auth_headers(api_key="clave-real-123"), "clave-real-123") is True
    assert is_mcp_request_authorized(_mcp_auth_headers(bearer="clave-real-123"), "clave-real-123") is True


def test_mcp_http_middleware_401_sin_key():
    """Middleware HTTP real: sin key → 401; con key → pasa (200)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from causadb.mcp.server import ApiKeyMiddleware

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    app.add_middleware(ApiKeyMiddleware, api_key="clave-real-123")
    client = TestClient(app, raise_server_exceptions=False)
    r1 = client.post("/mcp", json={})
    assert r1.status_code == 401, f"sin key debe ser 401, fue {r1.status_code}"
    assert "CAUSADB_MCP_API_KEY" in r1.text
    r2 = client.post("/mcp", json={}, headers={"X-API-Key": "mala"})
    assert r2.status_code == 401
    r3 = client.post("/mcp", json={}, headers={"X-API-Key": "clave-real-123"})
    assert r3.status_code == 200


def test_mcp_stdio_sin_key_sigue_ok(tmp_path):
    """stdio local SIN cambios: sin key igual funciona."""
    from causadb.mcp.server import create_server
    from tests.helpers._mcp_call import _call_tool
    ledger = str(tmp_path / "ledger.log")
    server = create_server(config_ledger_path=ledger)
    blocks, _ = _call_tool(server, "validate", {"ledger_path": ledger})
    text = "".join(getattr(b, "text", str(b)) for b in blocks)
    assert "is_valid" in text


def test_mcp_http_bindeo_por_defecto_loopback():
    """El default de host para MCP-HTTP debe ser 127.0.0.1."""
    import inspect
    from causadb.mcp import server as srv
    src = inspect.getsource(srv._parse_args)
    assert "127.0.0.1" in src, "bindeo por defecto debe ser 127.0.0.1"


# ---------------------------------------------------------------------------
# 3. REST serve sin auth NO arranca
# ---------------------------------------------------------------------------

def test_serve_sin_auth_no_arranca(monkeypatch, tmp_path):
    """cmd_serve start sin CAUSADB_API_KEY ni archivo → rc=1 con mensaje explicativo."""
    import argparse
    from causadb.cli import _cmd_serve
    from causadb._workspace import WorkspaceManager
    monkeypatch.delenv("CAUSADB_API_KEY", raising=False)
    monkeypatch.delenv("CAUSADB_API_KEY_FILE", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    WorkspaceManager.init(str(proj))
    ledger = str(proj / ".causadb" / "ledger.log")
    args = argparse.Namespace(action="start", ledger=ledger, host="127.0.0.1",
                              port=7457, daemon=False)
    code, out = _cmd_serve.cmd_serve(args)
    assert code == 1, f"serve sin auth debe fallar rápido, dio {code}: {out}"
    assert "CAUSADB_API_KEY" in out, "el mensaje debe documentar dónde vive la key"
    assert "0600" in out or "600" in out, "el mensaje debe mencionar archivo 0600"


# ---------------------------------------------------------------------------
# 4. Webhook TradingView FASE 1
# ---------------------------------------------------------------------------

@pytest.fixture
def webhook_server(tmp_path, monkeypatch):
    from causadb._init import causadb_init
    from causadb._rest_api import serve_in_thread
    monkeypatch.setenv("CAUSADB_WEBHOOK_SECRET", "secreto-largo-de-prueba-1234567890")
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0)
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


def _count_lines(path):
    with open(path) as f:
        return len(f.readlines())


def _post_raw(port, path, body_str, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    conn.request("POST", path, body_str, hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    conn.close()
    return resp.status, data


def test_webhook_secret_mal_404(webhook_server):
    """URL con secreto incorrecto → 404 (no revela existencia)."""
    _, port, _ = webhook_server
    payload = {"symbol": "BTCUSD", "side": "buy", "qty": 0.01, "price": 95000.0}
    status, _ = _post_raw(port, "/api/webhook/tradingview/secreto-malo",
                           json.dumps(payload))
    assert status == 404


def test_webhook_legacy_sin_secreto_404_cuando_hay_secreto(webhook_server):
    """Con secreto configurado, la URL legacy sin segmento → 404."""
    _, port, _ = webhook_server
    payload = {"symbol": "BTCUSD", "side": "buy", "qty": 0.01, "price": 95000.0}
    status, _ = _post_raw(port, "/api/webhook/tradingview", json.dumps(payload))
    assert status == 404


def test_webhook_body_vacio_400_y_nada_anexado(webhook_server):
    """Body vacío → 400 y NADA anexado al ledger."""
    ledger, port, _ = webhook_server
    before = _count_lines(ledger)
    status, _ = _post_raw(port, "/api/webhook/tradingview/secreto-largo-de-prueba-1234567890", "")
    assert status == 400, f"body vacío debe ser 400, fue {status}"
    assert _count_lines(ledger) == before, "body vacío NO debe persistir nada"


def test_webhook_body_invalido_400_y_nada_anexado(webhook_server):
    """Body inválido (sin campos requeridos) → 400 y NADA anexado."""
    ledger, port, _ = webhook_server
    before = _count_lines(ledger)
    status, _ = _post_raw(port, "/api/webhook/tradingview/secreto-largo-de-prueba-1234567890",
                           json.dumps({"foo": "bar"}))
    assert status == 400
    assert _count_lines(ledger) == before


def test_webhook_valido_200_y_anexa(webhook_server):
    """Body válido + secreto OK → 200 y anexa 1 evento."""
    ledger, port, _ = webhook_server
    before = _count_lines(ledger)
    payload = {"symbol": "BTCUSD", "side": "buy", "qty": 0.01, "price": 95000.0}
    status, data = _post_raw(port, "/api/webhook/tradingview/secreto-largo-de-prueba-1234567890",
                              json.dumps(payload))
    assert status == 200
    assert "event_id" in data
    assert _count_lines(ledger) == before + 1
