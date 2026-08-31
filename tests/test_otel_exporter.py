"""Tests para el exporter OTLP (F.6.2).

Artículo III — test-first: estos tests se escriben ANTES de la implementación.
Artículo IX — cada test debe romperse si se muta la implementación.

Decisiones del operador (NO MODIFICAR):
- `OTLPSpanExporter` oficial (no exporter custom).
- `export_ledger(ledger_path, endpoint, headers=None) -> dict` es FUNCIÓN, no clase.
- 0 skips — todos los tests OTel corren siempre.
- Mock HTTP via `http.server.ThreadingHTTPServer` del stdlib (Artículo VII).

Patrón de mock:
    Levantar un `ThreadingHTTPServer` en puerto efímero con un handler que
    captura los requests POST. No parseamos el body protobuf — solo verificamos
    que el request llegó y la cantidad. El contenido del span es trabajo del
    mapper (F.6.1), no del exporter.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from causadb.cli.main import main
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb.otel._exporter import export_ledger


# ---------------------------------------------------------------------------
# Mock OTLP HTTP server (stdlib — Artículo VII)
# ---------------------------------------------------------------------------

class _MockOTLPHandler(BaseHTTPRequestHandler):
    """Handler que captura los requests POST en una lista de clase.

    No parsea el body protobuf — solo registra que llegó un request.
    El status code se controla via `_MockOTLPHandler.response_status`
    (default 200). Para test 5 (error), se setea a 500 antes de llamar.
    """

    captured_requests: list = []
    response_status: int = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        _MockOTLPHandler.captured_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        self.send_response(_MockOTLPHandler.response_status)
        self.end_headers()
        # OTLP expects empty body on success
        self.wfile.write(b"")

    def log_message(self, *args, **kwargs):
        # Silenciar logs del mock server
        pass


def _start_mock_server():
    """Levanta un mock OTLP endpoint en puerto efímero. Devuelve (server, base_url)."""
    _MockOTLPHandler.captured_requests = []
    _MockOTLPHandler.response_status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOTLPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, base_url


def _stop_mock_server(server):
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# Helpers — construir ledgers con eventos específicos
# ---------------------------------------------------------------------------

def _make_ledger(tmp_path, events):
    """Crea un ledger en tmp_path/ledger.log y appenda los eventos dados."""
    ledger_path = str(tmp_path / "ledger.log")
    # LedgerWriter requiere path absoluto y archivo existente
    open(ledger_path, "w").close()
    writer = LedgerWriter(ledger_path)
    for ev in events:
        writer.append(ev)
    return ledger_path


def _llm_invoked():
    return CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"model": "gpt-4", "prompt": "hi", "response_tokens": 10},
    )


def _tool_called():
    return CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"tool_name": "read_file", "arguments": {"path": "/x"}},
    )


def _retrieval_done():
    return CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"query": "q", "chunks": ["c1", "c2"]},
    )


def _memory_op():
    return CanonicalEvent(
        event_type=EventType.MEMORY_OP,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"operation": "create", "key": "k"},
    )


def _agent_handoff():
    return CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"from_agent": "a", "to_agent": "b", "trace_id": "t"},
    )


def _file_modified():
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    )


def _command_run():
    return CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="ctx-1",
        source="opencode:agent1",
        payload={"command": "ls", "exit_code": 0},
    )


# ---------------------------------------------------------------------------
# Test 1 — LLM_INVOKED → 1 span exportado
# ---------------------------------------------------------------------------

def test_otel_exporter_exports_llm_invoked_as_chat_span(tmp_path):
    server, base_url = _start_mock_server()
    try:
        ledger_path = _make_ledger(tmp_path, [_llm_invoked()])
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        assert result["exported_spans"] == 1, f"expected 1, got {result}"
        assert result["skipped_unknown_types"] == 0, f"expected 0, got {result}"
        assert result["errors"] == 0, f"expected 0, got {result}"
        assert len(_MockOTLPHandler.captured_requests) == 1, (
            f"expected 1 HTTP request, got {len(_MockOTLPHandler.captured_requests)}"
        )
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 2 — TOOL_CALLED → 1 span exportado
# ---------------------------------------------------------------------------

def test_otel_exporter_exports_tool_called_as_execute_tool_span(tmp_path):
    server, base_url = _start_mock_server()
    try:
        ledger_path = _make_ledger(tmp_path, [_tool_called()])
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        assert result["exported_spans"] == 1, f"expected 1, got {result}"
        assert result["skipped_unknown_types"] == 0, f"expected 0, got {result}"
        assert result["errors"] == 0, f"expected 0, got {result}"
        assert len(_MockOTLPHandler.captured_requests) == 1, (
            f"expected 1 HTTP request, got {len(_MockOTLPHandler.captured_requests)}"
        )
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 3 — 5 mapeados + 2 físicos → 5 spans, 2 skipped, 1 request
# ---------------------------------------------------------------------------

def test_otel_exporter_counts_spans_correctly(tmp_path):
    server, base_url = _start_mock_server()
    try:
        events = [
            _llm_invoked(),
            _tool_called(),
            _retrieval_done(),
            _memory_op(),
            _agent_handoff(),
            _file_modified(),   # físico — skip
            _command_run(),    # físico — skip
        ]
        ledger_path = _make_ledger(tmp_path, events)
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        assert result["exported_spans"] == 5, f"expected 5, got {result}"
        assert result["skipped_unknown_types"] == 2, f"expected 2, got {result}"
        assert result["errors"] == 0, f"expected 0, got {result}"
        # Un solo request HTTP con los 5 spans (export se llama una vez)
        assert len(_MockOTLPHandler.captured_requests) == 1, (
            f"expected 1 HTTP request (batch of 5), got {len(_MockOTLPHandler.captured_requests)}"
        )
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 4 — Solo físicos → 0 spans, 2 skipped, 0 requests
# ---------------------------------------------------------------------------

def test_otel_exporter_skips_physical_event_types(tmp_path):
    server, base_url = _start_mock_server()
    try:
        ledger_path = _make_ledger(tmp_path, [_file_modified(), _command_run()])
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        assert result["exported_spans"] == 0, f"expected 0, got {result}"
        assert result["skipped_unknown_types"] == 2, f"expected 2, got {result}"
        assert result["errors"] == 0, f"expected 0, got {result}"
        # No se envió ningún request porque no hay spans
        assert len(_MockOTLPHandler.captured_requests) == 0, (
            f"expected 0 HTTP requests, got {len(_MockOTLPHandler.captured_requests)}"
        )
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 5 — Mock devuelve 500 → errors=1, exported_spans=0
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.timeout(30)
def test_otel_exporter_handles_otlp_error(tmp_path):
    server, base_url = _start_mock_server()
    _MockOTLPHandler.response_status = 500
    try:
        ledger_path = _make_ledger(tmp_path, [_llm_invoked()])
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        assert result["exported_spans"] == 0, (
            f"expected 0 on export failure, got {result}"
        )
        assert result["errors"] == 1, f"expected 1, got {result}"
        assert result["skipped_unknown_types"] == 0, f"expected 0, got {result}"
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 6 — E2E CLI: `causadb export --format otel --ledger --endpoint`
# ---------------------------------------------------------------------------

def test_cli_export_otel_outputs_json_summary(tmp_path, capsys):
    server, base_url = _start_mock_server()
    try:
        # init workspace
        workspace = str(tmp_path / "ws")
        rc_init = main(["init", workspace])
        assert rc_init == 0
        capsys.readouterr()  # flush init output

        ledger_path = os.path.join(workspace, "ledger.log")
        writer = LedgerWriter(ledger_path)
        writer.append(_llm_invoked())

        rc = main([
            "export",
            "--format", "otel",
            "--ledger", ledger_path,
            "--endpoint", base_url + "/v1/traces",
        ])
        captured = capsys.readouterr()
        assert rc == 0, f"expected exit 0, got {rc}; stdout={captured.out!r}"

        payload = json.loads(captured.out)
        assert "exported_spans" in payload
        assert "skipped_unknown_types" in payload
        assert "errors" in payload
        assert payload["exported_spans"] == 1, f"expected 1, got {payload}"
        assert payload["errors"] == 0, f"expected 0, got {payload}"
    finally:
        _stop_mock_server(server)


# ---------------------------------------------------------------------------
# Test 7 — Anti-teatro: si export() real no se llama, los tests fallan
# ---------------------------------------------------------------------------

def test_anti_teatro_otel_exporter_skips_real_export(tmp_path):
    """Anti-teatro (Artículo IX): si `export_ledger` skipea el `export()` real,
    este test debe detectarlo.

    Mutación: monkey-patch `OTLPSpanExporter.export` para retornar
    `SpanExportResult.FAILURE` sin llamar HTTP. Verifica:
      - result["exported_spans"] == 0 (porque export falló)
      - captured == [] (no llegó ningún request — el mock HTTP no se llamó)
      - result["errors"] == 1

    Restaura el método original al final (try/finally).
    """
    server, base_url = _start_mock_server()
    original_export = OTLPSpanExporter.export
    try:
        # Mutación: export() retorna FAILURE sin llamar HTTP
        def _broken_export(self, spans):
            return SpanExportResult.FAILURE

        OTLPSpanExporter.export = _broken_export

        events = [
            _llm_invoked(),
            _tool_called(),
            _retrieval_done(),
            _memory_op(),
            _agent_handoff(),
        ]
        ledger_path = _make_ledger(tmp_path, events)
        result = export_ledger(ledger_path, endpoint=base_url + "/v1/traces")

        # Con export() roto: 0 spans exportados, errors=1
        assert result["exported_spans"] == 0, (
            f"expected 0 with broken export, got {result}"
        )
        assert result["errors"] == 1, f"expected 1, got {result}"
        # El mock HTTP no fue llamado porque export() no hizo HTTP
        assert len(_MockOTLPHandler.captured_requests) == 0, (
            f"expected 0 HTTP requests with broken export, "
            f"got {len(_MockOTLPHandler.captured_requests)}"
        )
    finally:
        # Restaurar el método original
        OTLPSpanExporter.export = original_export
        _stop_mock_server(server)
