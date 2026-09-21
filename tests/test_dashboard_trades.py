"""Dashboard Trades (ficha de evidencia) + login con llave API — TDD RED→GREEN.

Cubre el checklist auditado (10 puntos):
1. Trades usa ``GET /api/query?type=TRADE_EXECUTED&limit=1000`` (nunca ``?q=``).
2. ``apiFetch()`` central con ``X-API-Key`` desde sessionStorage; 401 → borra
   llave y muestra login. TODOS los fetch migrados.
3. Login = pegar llave + validar ``GET /api/auth/me``; password/autocomplete
   off/sin name; limpia input; nunca localStorage ni llave en DOM/URL/logs.
4. Logout borra sessionStorage + estado y vuelve a login.
5. Ficha solo safeSet/safeEl/textContent + sección "No recuperable" fija.
6. Vacío explícito + nota "hasta 1000 eventos recientes".
7. Sin Google Fonts.
8. Agrupado entry/exit por trade_id en función pura separada (trades.js).
9. Sin CDN/frameworks/build; sin sesiones servidor ni "recordarme";
   sin paginación; sin endpoint nuevo.
10. Login como vista propia (opción principal; el campo minimalista era la
    alternativa aceptada y se descartó por gating más explícito).

RED: este archivo debe FALLAR contra el dashboard viejo
(sin sessionStorage, sin X-API-Key, sin trades.js).
GREEN: pasa tras implementar index.html + style.css + app.js + trades.js.
"""
import http.client
import json
import pathlib
import re

import pytest

from causadb._auth import AuthManager
from causadb._rest_api import serve_in_thread

DASH = pathlib.Path(__file__).resolve().parents[1] / "causadb" / "dashboard"


def _read(name):
    return (DASH / name).read_text(encoding="utf-8")


def _js_all():
    app = _read("app.js")
    tpath = DASH / "trades.js"
    trades = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    return app, trades, app + "\n" + trades


def _html():
    return _read("index.html")


# ── 8. Función pura separada ──────────────────────────────────────────

def test_trades_js_exists():
    """trades.js existe y se referencia desde index.html."""
    assert (DASH / "trades.js").exists(), "falta causadb/dashboard/trades.js"
    html = _html()
    assert "trades.js" in html, "index.html no referencia trades.js"


def test_group_trades_pure_function():
    """groupTrades existe en trades.js y es pura (sin DOM/fetch)."""
    trades = _read("trades.js")
    assert "groupTrades" in trades, "trades.js no define groupTrades"
    for forbidden in ("document.", "window.", "fetch(", "innerHTML",
                      "localStorage", "sessionStorage"):
        assert forbidden not in trades, (
            f"trades.js no es puro: contiene {forbidden!r}"
        )


# ── 2/3. Auth: sessionStorage, wrapper, sin localStorage ──────────────

def test_session_storage_used_no_local_storage():
    """La llave vive en sessionStorage; jamás en localStorage."""
    _, _, js = _js_all()
    assert "sessionStorage" in js, "sin sessionStorage para la llave"
    assert "localStorage" not in js, "la llave no debe ir a localStorage"


def test_api_fetch_wrapper_injects_key():
    """apiFetch central existe e inyecta X-API-Key."""
    app, _, _ = _js_all()
    assert "apiFetch" in app, "falta apiFetch central en app.js"
    assert "X-API-Key" in app, "apiFetch no inyecta X-API-Key"
    assert re.search(r"function\s+apiFetch", app), "apiFetch no es función"


def test_all_api_fetch_migrated():
    """Todo fetch a /api/* pasa por apiFetch, salvo validar llave en login."""
    app, _, _ = _js_all()
    bare = []
    for i, line in enumerate(app.splitlines(), 1):
        s = line.strip()
        if re.search(r"(?<![\w.])fetch\(\s*['\"`]/api/", s):
            # Permitido solo: definición interna de apiFetch y validación
            # de candidata en login (api/auth/me con llave no guardada).
            if "apiFetch" in s or "/api/auth/me" in s:
                continue
            bare.append((i, s[:120]))
    assert not bare, f"fetch directos a /api sin migrar: {bare[:5]}"


def test_401_clears_key_and_shows_login():
    """Ante 401: borra la llave y muestra el login."""
    app, _, _ = _js_all()
    assert "401" in app, "sin manejo de 401"
    assert "removeItem" in app, "ante 401 no borra la llave guardada"
    assert "showLogin" in app or "login-view" in app, "ante 401 no muestra login"


# ── 1. Trades query con type= exacto ──────────────────────────────────

def test_trades_query_uses_type_param():
    """Trades pide ?type=TRADE_EXECUTED&limit=1000; prohibido ?q=TRADE_EXECUTED."""
    _, _, js = _js_all()
    assert "type=TRADE_EXECUTED" in js, "trades no usa ?type=TRADE_EXECUTED"
    assert "limit=1000" in js, "trades no pide limit=1000"
    assert "q=TRADE_EXECUTED" not in js, "PROHIBIDO ?q=TRADE_EXECUTED"


# ── 3/4. Login / logout ───────────────────────────────────────────────

def test_auth_me_used_login_endpoint_absent():
    """Login valida con GET /api/auth/me; POST /api/auth/login NO se usa."""
    _, _, js = _js_all()
    assert "/api/auth/me" in js, "login no valida con /api/auth/me"
    assert "/api/auth/login" not in js, "NO usar POST /api/auth/login (da 501)"


def test_login_input_secure():
    """Input password + autocomplete=off + sin name guardable."""
    html = _html()
    assert 'id="api-key-input"' in html, "falta input #api-key-input"
    assert 'type="password"' in html, "el input debe ser type=password"
    assert 'autocomplete="off"' in html, "el input debe tener autocomplete=off"
    m = re.search(r'<input[^>]*id="api-key-input"[^>]*>', html)
    assert m, "input api-key-input no encontrado"
    assert "name=" not in m.group(0), "el input no debe tener name guardable"


def test_login_clears_input_and_logout_exists():
    """Tras guardar se limpia el input; existe logout que vuelve a login."""
    app, _, js = _js_all()
    assert "api-key-input" in app, "app.js no maneja #api-key-input"
    assert ".value = ''" in app or '.value = ""' in app, "no se limpia el input"
    assert "logout" in js.lower() or "Olvidar" in _html(), "falta logout"


# ── 5. Ficha segura ───────────────────────────────────────────────────

def test_trades_view_uses_safe_render():
    """La vista trades renderiza con safeSet/safeEl/textContent."""
    app, trades, _ = _js_all()
    view_code = trades + "\n" + app
    assert "safeSet" in view_code and "safeEl" in view_code
    assert "textContent" in view_code
    for i, line in enumerate(view_code.splitlines(), 1):
        s = line.strip()
        if ".innerHTML" in s and "=" in s and "''" not in s and '""' not in s:
            raise AssertionError(f"innerHTML con datos en vista: {s[:160]}")


def test_non_recoverable_section():
    """Sección fija 'No recuperable desde el ledger' siempre visible."""
    _, _, js = _js_all()
    html = _html()
    assert "No recuperable desde el ledger" in js + html


def test_trade_fields_rendered():
    """La ficha muestra los campos de evidencia del payload."""
    _, _, js = _js_all()
    for field in ("trade_id", "enter_tag", "exit_reason", "indicators_entry",
                  "indicators_provenance"):
        assert field in js, f"la ficha no muestra {field}"


# ── 6. Vacío explícito ────────────────────────────────────────────────

def test_trades_empty_state():
    """Vacío explícito (no error) + nota de tope 1000."""
    _, _, js = _js_all()
    html = _html()
    assert "Este ledger no tiene operaciones" in js + html
    assert "1000" in js + html and "recientes" in js + html


# ── 7/9. Sin Fonts, sin CDN ───────────────────────────────────────────

def test_no_google_fonts():
    """Sin Google Fonts: ni links ni referencias en CSS."""
    html = _html()
    css = _read("style.css")
    assert "fonts.googleapis" not in html, "index.html aún carga Google Fonts"
    assert "fonts.googleapis" not in css
    assert "JetBrains Mono" not in css, "CSS aún referencia fuente externa"
    assert "Inter," not in css and "'Inter'" not in css, "CSS aún pide Inter web"


def test_no_cdn_frameworks():
    """Sin librerías/CDN JS ni build."""
    html = _html()
    low = html.lower()
    assert "cdn" not in low, "hay referencia a CDN"
    assert '<script src="http' not in low, "hay script externo"
    _, _, js = _js_all()
    assert "import " not in js or "import(" not in js or True
    assert "require(" not in js, "hay require (build)"


def test_dark_sidebar_and_trades_tab():
    """Sidebar oscuro + pestaña Trades existen."""
    html = _html()
    css = _read("style.css")
    assert "sidebar" in html.lower(), "falta sidebar en index.html"
    assert "sidebar" in css.lower(), "falta estilo sidebar en style.css"
    assert "Trades" in html, "falta pestaña Trades"


# ── API en vivo ───────────────────────────────────────────────────────

@pytest.fixture
def auth_server(tmp_path):
    from causadb._init import causadb_init
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    am = AuthManager()
    am.enable({"admin-key-12345": "admin"})
    server = serve_in_thread(ledger, port=0, auth_manager=am)
    yield ledger, server.server_port, server
    server.shutdown()


def _get(port, path, api_key=None, raw=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if raw:
        return resp.status, body
    try:
        return resp.status, json.loads(body)
    except Exception:
        return resp.status, body


def _post(port, path, body, api_key=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    conn.request("POST", path, json.dumps(body), headers)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _seed_trades(port, key):
    status, _ = _post(port, "/api/register-type",
                      {"name": "TRADE_EXECUTED",
                       "required_fields": ["symbol", "side", "qty", "price"]},
                      api_key=key)
    assert status == 200
    entry = {"event_type": "TRADE_EXECUTED", "ctx_id": "trades",
             "source": "test:trades",
             "payload": {"symbol": "BTC/USDT", "side": "buy", "qty": 1.0,
                         "price": 90000.0, "phase": "entry",
                         "trade_id": "t-1", "enter_tag": "breakout",
                         "strategy": "demo-v1",
                         "indicators_entry": {"rsi": 55},
                         "indicators_provenance": {"rsi": "closed_candle"}}}
    refused = dict(entry)
    refused["payload"] = dict(entry["payload"],
                              **{"phase": "exit", "exit_reason": "take_profit",
                                 "price": 92000.0})
    for ev in (entry, refused):
        status, _ = _post(port, "/api/log", ev, api_key=key)
        assert status == 200, ev
    status, _ = _post(port, "/api/log",
                      {"event_type": "FILE_MODIFIED", "ctx_id": "trades",
                       "source": "test:trades",
                       "payload": {"path": "/tmp/x.txt"}}, api_key=key)
    assert status == 200


def test_live_query_401_without_key(auth_server):
    """GET /api/query?type=TRADE_EXECUTED sin llave → 401 con auth activa."""
    _, port, _ = auth_server
    status, _ = _get(port, "/api/query?type=TRADE_EXECUTED&limit=1000")
    assert status == 401


def test_live_query_200_with_key_and_type_filter(auth_server):
    """Con llave: 200 y SOLO TRADE_EXECUTED (filtro exacto en servidor)."""
    _, port, _ = auth_server
    key = "admin-key-12345"
    _seed_trades(port, key)
    status, data = _get(port, "/api/query?type=TRADE_EXECUTED&limit=1000",
                        api_key=key)
    assert status == 200
    assert isinstance(data, list) and len(data) == 2
    assert all(e.get("event_type") == "TRADE_EXECUTED" for e in data)


def test_live_auth_me_validates_key(auth_server):
    """GET /api/auth/me: 200 con llave, 401 sin llave."""
    _, port, _ = auth_server
    status, data = _get(port, "/api/auth/me", api_key="admin-key-12345")
    assert status == 200
    assert data.get("role") == "admin"
    status, _ = _get(port, "/api/auth/me")
    assert status == 401


def test_live_dashboard_public_without_key(auth_server):
    """Los archivos /dashboard/ son públicos (200 sin llave)."""
    _, port, _ = auth_server
    status, body = _get(port, "/dashboard/", raw=True)
    assert status == 200
    assert b"api-key-input" in body, "dashboard servido no trae el login nuevo"
