"""TDD RED — HTTP query hardening (spec auditada).

1. Remoto sin payloads por defecto (slim, con hash/path para pedir detalle)
   + mensaje envelope "slim by default, pass include_payloads=true".
2. Ledger ajeno -> 403 wrong-ledger (fail-closed, NO silencio).
3. Opt-in include_payloads=true resuelve (blobs/contenido completo).

NO toca bloque 1005-1036 de mcp/server.py (otro frente).
NO cambia default de _tools.causadb_query (stdio intacto).
"""
import inspect
import json

import anyio

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb.mcp.server import _apply_http_security, create_server
from causadb._config import CausaDBConfig
from tests.helpers._mcp_call import _call_tool, _error_message


def _text(blocks):
    return "".join(getattr(b, "text", str(b)) for b in blocks)


def _setup_ledger(tmp_path):
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="test",
        payload={
            "path": "/foo",
            "action": "create",
            "content_hash": "abc123def456",
            "content": "heavy-content-" * 20,
        },
    ))
    return ledger_path


def _http_server(ledger_path):
    server = create_server(config_ledger_path=ledger_path)
    config = CausaDBConfig(ledger_path=ledger_path)
    anyio.run(_apply_http_security, server, config)
    return server


def test_http_query_slim_by_default_with_trace_keys(tmp_path):
    """Remoto sin include_payloads -> slim por defecto, con hash/path,
    y mensaje avisando slim by default."""
    ledger_path = _setup_ledger(tmp_path)
    server = _http_server(ledger_path)

    blocks, _ = _call_tool(server, "query", {"ledger_path": ledger_path})
    data = json.loads(_text(blocks))
    assert isinstance(data, dict) and "events" in data
    assert len(data["events"]) == 1
    payload = data["events"][0]["event"]["payload"]
    # Slim: preserva trazabilidad...
    assert payload.get("path") == "/foo", f"slim debe preservar path, got {payload}"
    assert payload.get("content_hash") == "abc123def456", (
        f"slim debe preservar content_hash, got {payload}"
    )
    # ...pero NO el contenido pesado.
    assert "content" not in payload, (
        f"slim por defecto no debe traer content pesado, got {payload}"
    )
    # Envelope avisa slim by default.
    msg = data.get("message", "")
    assert "slim by default" in msg.lower(), f"mensaje debe avisar slim, got {msg!r}"
    assert "include_payloads=true" in msg.lower(), f"mensaje debe decir opt-in, got {msg!r}"


def test_http_query_foreign_ledger_403(tmp_path):
    """ledger_path del cliente != default -> 403 wrong-ledger (fail-closed)."""
    ledger_path = _setup_ledger(tmp_path)
    server = _http_server(ledger_path)
    foreign = str(tmp_path / "ajeno.log")

    try:
        _call_tool(server, "query", {"ledger_path": foreign})
    except Exception as exc:
        msg = _error_message(exc).lower()
        assert "403" in msg, f"debe ser 403, got {msg!r}"
        assert "wrong-ledger" in msg, f"debe decir wrong-ledger, got {msg!r}"
    else:
        raise AssertionError("ledger ajeno debe fallar 403, no silencio")


def test_http_query_optin_true_resolves(tmp_path):
    """Opt-in include_payloads=true -> contenido completo resuelto."""
    ledger_path = _setup_ledger(tmp_path)
    server = _http_server(ledger_path)

    blocks, _ = _call_tool(
        server, "query", {"ledger_path": ledger_path, "include_payloads": True}
    )
    data = json.loads(_text(blocks))
    assert len(data["events"]) == 1
    payload = data["events"][0]["event"]["payload"]
    assert payload.get("content", "").startswith("heavy-content-"), (
        f"opt-in true debe resolver contenido, got {payload}"
    )


def test_stdio_query_default_still_true():
    """Guardia: default de _tools.causadb_query y wrapper stdio intactos."""
    from causadb.mcp import _tools
    sig = inspect.signature(_tools.causadb_query)
    assert sig.parameters["include_payloads"].default is True
    server = create_server(config_ledger_path="/tmp/x.log")
    tool = server._tool_manager.get_tool("query")
    assert tool.fn_metadata.arg_model.model_fields["include_payloads"].default is True
