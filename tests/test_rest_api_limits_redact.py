"""TDD RED→GREEN — límites de listados + redacción en lectura + fail-closed.

Specs auditadas (BIT-CHR.35 P3 + Art. IX):
1. ``serve()`` fail-closed: ``auth_manager None`` → crear o levantar, salvo
   flag explícito solo-tests ``allow_unauthenticated_localhost``.
2. Redacción en LECTURA con ``_redactor`` existente (eximir
   ``GET /api/auth/me``; el dashboard verá hash, documentado).
3. TODOS los listados con limit default + clamp MAX + ``limit=0``→default
   + offset máximo.
"""
import json
import http.client
import pytest

from causadb._rest_api import serve_in_thread
from causadb._event_types import EventType


@pytest.fixture
def ledger_and_server(tmp_path):
    from causadb._init import causadb_init
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    # serve_in_thread es helper de tests: por defecto permite localhost sin
    # auth (escape hatch explícito). El fail-closed se testea aparte.
    server = serve_in_thread(ledger, port=0)
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


def _post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _log_events(port, n=5):
    ids = []
    for i in range(n):
        status, data = _post(port, "/api/log", {
            "event_type": EventType.FILE_MODIFIED.value,
            "ctx_id": "limits",
            "source": "rest:limits",
            "payload": {"path": f"/tmp/file{i}.txt"},
        })
        assert status == 200
        ids.append(data["event_id"])
    return ids


# ── 1. Helpers de límite (unit) ─────────────────────────────────────

def test_parse_limit_capping():
    """_parse_limit: None→default, <=0→default, >MAX→clamp, inválido→default."""
    from causadb._rest_api import _parse_limit
    from causadb._ledger_index import DEFAULT_QUERY_LIMIT, MAX_QUERY_LIMIT
    assert _parse_limit(None) == DEFAULT_QUERY_LIMIT
    assert _parse_limit("0") == DEFAULT_QUERY_LIMIT
    assert _parse_limit("-1") == DEFAULT_QUERY_LIMIT
    assert _parse_limit("1000000000") == MAX_QUERY_LIMIT
    assert _parse_limit("2") == 2
    assert _parse_limit("abc") == DEFAULT_QUERY_LIMIT


def test_parse_offset_capping():
    """_parse_offset: None→0, <0→0, >MAX→clamp, inválido→0."""
    from causadb._rest_api import _parse_offset
    from causadb._ledger_index import MAX_QUERY_LIMIT
    assert _parse_offset(None) == 0
    assert _parse_offset("0") == 0
    assert _parse_offset("-5") == 0
    assert _parse_offset("1000000000") == MAX_QUERY_LIMIT
    assert _parse_offset("10") == 10
    assert _parse_offset("abc") == 0


# ── 2. serve() fail-closed (auth None directo no expone switch/daemon) ──

def test_serve_fail_closed_no_auth(tmp_path, monkeypatch):
    """serve_in_thread con auth_manager=None y flag=False → levanta (fail-closed).

    Sin auth y sin el escape hatch explícito, el server NO arranca → los
    endpoints de switch/daemon nunca quedan expuestos sin autenticar.
    """
    from causadb._init import causadb_init
    from causadb._rest_api import serve_in_thread
    import causadb._auth as auth_mod
    monkeypatch.setattr(auth_mod, "load_rest_api_key", lambda: None)
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    with pytest.raises(RuntimeError):
        serve_in_thread(ledger, port=0, allow_unauthenticated_localhost=False)


def test_serve_allow_unauthenticated_localhost_flag(tmp_path):
    """serve_in_thread con flag=True arranca sin auth (escape hatch solo-tests)."""
    from causadb._init import causadb_init
    from causadb._rest_api import serve_in_thread
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0, allow_unauthenticated_localhost=True)
    try:
        port = server.server_port
        status, data = _get(port, "/api/daemon/status")
        assert status == 200
        assert "running" in data
    finally:
        server.shutdown()


def test_build_auth_or_error_no_key(monkeypatch):
    """_build_auth_or_error sin key → (None, error) (fail-closed)."""
    from causadb._rest_api import _build_auth_or_error
    import causadb._auth as auth_mod
    monkeypatch.setattr(auth_mod, "load_rest_api_key", lambda: None)
    am, err = _build_auth_or_error()
    assert am is None
    assert err is not None


# ── 3. GET /api/query — limit default + clamp + limit=0→default ─────

def test_get_query_limit_zero_means_default(ledger_and_server):
    """GET /api/query?limit=0 → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/query?limit=0")
    assert status == 200
    assert len(data) >= 1


def test_get_query_limit_negative_means_default(ledger_and_server):
    """GET /api/query?limit=-1 → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/query?limit=-1")
    assert status == 200
    assert len(data) >= 1


def test_get_query_limit_huge_clamped(ledger_and_server):
    """GET /api/query?limit=1e9 → clamp a MAX_QUERY_LIMIT."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/query?limit=1000000000")
    assert status == 200
    assert len(data) <= 1000


def test_get_query_limit_applies(ledger_and_server):
    """GET /api/query?limit=2 → exactamente 2 eventos."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/query?limit=2")
    assert status == 200
    assert len(data) == 2


# ── 4. POST /api/query — limit default + clamp + limit=0→default ────

def test_post_query_limit_applies(ledger_and_server):
    """POST /api/query {limit:2} → exactamente 2 (hoy ignora limit → RED)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/query", {"limit": 2})
    assert status == 200
    assert len(data) == 2


def test_post_query_limit_zero_means_default(ledger_and_server):
    """POST /api/query {limit:0} → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/query", {"limit": 0})
    assert status == 200
    assert len(data) >= 1


def test_post_query_limit_negative_means_default(ledger_and_server):
    """POST /api/query {limit:-1} → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/query", {"limit": -1})
    assert status == 200
    assert len(data) >= 1


def test_post_query_limit_huge_clamped(ledger_and_server):
    """POST /api/query {limit:1e9} → clamp a MAX_QUERY_LIMIT."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/query", {"limit": 1000000000})
    assert status == 200
    assert len(data) <= 1000


# ── 5. GET /api/events — limit default + clamp + offset máximo ──────

def test_get_events_limit_applies(ledger_and_server):
    """GET /api/events?limit=2 → exactamente 2 eventos."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/events?limit=2")
    assert status == 200
    assert len(data) == 2


def test_get_events_limit_zero_means_default(ledger_and_server):
    """GET /api/events?limit=0 → default (NO infinito, NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/events?limit=0")
    assert status == 200
    assert len(data) >= 1
    assert len(data) <= 1000


def test_get_events_limit_negative_means_default(ledger_and_server):
    """GET /api/events?limit=-1 → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/events?limit=-1")
    assert status == 200
    assert len(data) >= 1


def test_get_events_limit_huge_clamped(ledger_and_server):
    """GET /api/events?limit=1e9 → clamp a MAX_QUERY_LIMIT."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/events?limit=1000000000")
    assert status == 200
    assert len(data) <= 1000


def test_get_events_offset_clamped(ledger_and_server):
    """GET /api/events?offset=1e9 → clamp (no crash, lista)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _get(port, "/api/events?offset=1000000000")
    assert status == 200
    assert isinstance(data, list)


# ── 6. POST /api/export — limit default + clamp ────────────────────

def test_export_limit_zero_means_default(ledger_and_server):
    """POST /api/export {limit:0} → default (NO vacío)."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/export", {"limit": 0})
    assert status == 200
    assert isinstance(data, list)
    assert len(data) >= 1


def test_export_limit_huge_clamped(ledger_and_server):
    """POST /api/export {limit:1e9} → clamp a MAX_QUERY_LIMIT."""
    _, port, _ = ledger_and_server
    _log_events(port, 5)
    status, data = _post(port, "/api/export", {"limit": 1000000000})
    assert status == 200
    assert isinstance(data, list)
    assert len(data) <= 1000


# ── 7. POST /api/trace — limit sin crash ────────────────────────────

def test_trace_limit_huge(ledger_and_server):
    """POST /api/trace {limit:1e9} → no crash, evento encontrado."""
    _, port, _ = ledger_and_server
    status, log_data = _post(port, "/api/log", {
        "event_type": EventType.FILE_MODIFIED.value,
        "ctx_id": "trace",
        "source": "rest:limits",
        "payload": {"path": "/tmp/x.txt"},
    })
    assert status == 200
    eid = log_data["event_id"]
    status, data = _post(port, "/api/trace", {"event_id": eid, "limit": 1000000000})
    assert status == 200
    assert data["event"]["event_id"] == eid


# ── 8. Redacción en LECTURA ─────────────────────────────────────────

def test_read_redaction_legacy_event(ledger_and_server):
    """Evento legacy (sin redactar en escritura) se redacta en LECTURA.

    Simula un evento persistido ANTES de la redacción (forward-only): se
    inyecta con ``redaction_enabled=False`` y se lee vía API. El secreto
    NO debe aparecer en claro en la respuesta.
    """
    ledger, port, _ = ledger_and_server
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._config import CausaDBConfig
    cfg = CausaDBConfig(ledger_path=ledger)
    cfg.redaction_enabled = False  # simula evento legacy sin redactar
    writer = LedgerWriter(ledger, config=cfg)
    writer.append(CanonicalEvent(
        event_type=EventType("FILE_MODIFIED"),
        ctx_id="legacy",
        source="rest:legacy",
        source_type="agent",
        payload={"path": "/tmp/legacy.txt", "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456"},
    ))

    status, data = _get(port, "/api/query")
    assert status == 200
    blob = json.dumps(data)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob


def test_dashboard_no_secrets_in_clear(ledger_and_server):
    """Dashboard (query/events) sin sk-/ghp-/AKIA en claro (regresión)."""
    _, port, _ = ledger_and_server
    _post(port, "/api/log", {
        "event_type": EventType.FILE_MODIFIED.value,
        "ctx_id": "secrets",
        "source": "rest:limits",
        "payload": {
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
            "token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
            "aws": "AKIAIOSFODNN7EXAMPLE",
        },
    })
    for path in ("/api/query", "/api/events"):
        status, data = _get(port, path)
        assert status == 200
        blob = json.dumps(data)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in blob
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij" not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob