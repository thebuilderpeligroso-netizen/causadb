"""Tests para C1/C2/C3 — Caps de bytes server-side en tools MCP.

Test-First (Art. III): estos tests se escriben ANTES de la implementación.

Cubren:
  1. Envelope SIEMPRE presente en ``query`` (``{"events", "truncated",
     "bytes", "dropped_events", "message"}``) — nunca array pelado.
  2. Cap de bytes ``MAX_RESPONSE_BYTES`` (env ``CAUSADB_MAX_RESPONSE_BYTES``).
  3. Slim fallback: si el PRIMER evento excede el cap → degradar a ficha
     slim (``include_payloads=false``), nunca devolver vacío.
  4. Cap de bytes en revive (C2) — trim a nivel de datos de
     ``governance_decisions`` (newest-first) + aviso. ``write_path`` sin cap.
  5. Cap de bytes en feedback/stream/ocb_load_partition + resource
     ``causadb://events`` (mismo path de datos, mismo riesgo).

Anti-teatro (Art. IX): cada test falla si el cap no existe o está mal
ubicado (envelope ausente, array pelado, evento gigante perdido, cap que
no respeta el env, etc.).
"""
import argparse
import json
import os
from types import MappingProxyType

import pytest
import anyio

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from tests.helpers._mcp_call import _call_tool
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import cmd_revive
from causadb.mcp import _tools
from causadb.mcp.server import create_server


# ---------------------------------------------------------------------------
# Helpers (mismos patrones que test_mcp_query_cap.py / test_revive_r24.py)
# ---------------------------------------------------------------------------

def _text(content_blocks):
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


def _make_big_payload(idx: int, size: int = 1_000_000) -> dict:
    """Payload de ~1MB → se externaliza a ``$blob`` (threshold 1024) y se
    resuelve inline en ``query(include_payloads=True)``. Simula el caso
    real TOOL_CALLED/REASONING_STEP con payloads gigantes."""
    return {
        "path": f"/big/{idx}",
        "action": "create",
        "content": "y" * size,
        "idx": idx,
    }


def _write_events(ledger_path, n, payload_factory,
                  event_type=EventType.FILE_MODIFIED):
    writer = LedgerWriter(ledger_path)
    for i in range(n):
        writer.append(CanonicalEvent(
            event_type=event_type,
            ctx_id="ctx",
            source="test",
            payload=payload_factory(i),
        ))
    return ledger_path


def _make_revive_args(ledger, fmt="markdown", write=None, decisions=10):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    argv = ["--ledger", ledger, "--format", fmt, "--decisions", str(decisions)]
    if write:
        argv += ["--write", write]
    return parser.parse_args(argv)


def _make_ledger_with_decisions(tmp_path, decisions_spec):
    """Patrón de test_revive_r24.py: ledger con GOVERNANCE_DECISION +
    STATUS_CHANGED. El replay las acumula append-order; revive las invierte
    (newest-first)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    for impact, decision_type, origin, reasoning, status in decisions_spec:
        gd_event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "reasoning": reasoning,
                "impact": impact,
                "decision_type": decision_type,
                "origin": origin,
            }),
        )
        written = writer.append(gd_event)
        gd_event_id = written["event"]["event_id"]

        if status:
            status_event = CanonicalEvent(
                event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
                ctx_id="test",
                source="causadb:test",
                parent_event_id=gd_event_id,
                payload=MappingProxyType({"new_status": status}),
            )
            writer.append(status_event)
    return ledger


def _make_decision_specs(n: int) -> list:
    """n decisiones con reasoning de 50KB, reasoning distinguible por índice
    (``DECISION-{i}`` en los primeros 200 chars — el renderer markdown
    trunca a 200)."""
    return [
        ("high", "strategic", "agent", f"DECISION-{i}-" + "x" * 50000, "proposed")
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. Envelope siempre presente en query
# ---------------------------------------------------------------------------

def test_query_byte_cap_truncates_large_payloads(tmp_path):
    """1 evento de 1MB → envelope, ``truncated:true``, ``bytes`` ≤ cap + 100.

    Anti-teatro: una impl sin cap devuelve el evento completo (1MB > cap)
    y la aserción de bytes falla; una impl sin envelope devuelve array y
    la aserción de dict falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 1, _make_big_payload)

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    data = json.loads(_text(content_blocks))

    # Envelope siempre presente (nunca array pelado).
    assert isinstance(data, dict), f"envelope dict esperado, got {type(data)}"
    for key in ("events", "truncated", "bytes", "dropped_events", "message"):
        assert key in data, f"envelope debe tener la key '{key}'"

    assert data["truncated"] is True
    cap = _tools.MAX_RESPONSE_BYTES
    assert data["bytes"] <= cap + 100, (
        f"bytes={data['bytes']} debe ser <= cap({cap})+100"
    )
    assert isinstance(data["events"], list)


def test_query_byte_cap_under_limit_no_truncation(tmp_path):
    """Ledger chico → ``truncated:false``, todos los eventos, envelope completo.

    Anti-teatro: una impl que trunca por defecto pierde eventos y falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 3, lambda i: {"path": f"/f{i}", "action": "create"})

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    data = json.loads(_text(content_blocks))

    assert isinstance(data, dict)
    assert data["truncated"] is False
    assert data["dropped_events"] == 0
    assert len(data["events"]) == 3
    assert data["message"] == ""


def test_query_byte_cap_preserves_order_and_count(tmp_path):
    """100 eventos chicos → todos, sin falso positivo, orden preservado.

    Anti-teatro: un cap mal calibrado (trunca aunque no haga falta) pierde
    eventos y falla; un cap que desordena falla la aserción de paths.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 100, lambda i: {
        "path": f"/f{i}", "action": "create", "idx": i,
    })

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    data = json.loads(_text(content_blocks))

    assert data["truncated"] is False
    assert len(data["events"]) == 100
    paths = [e["event"]["payload"]["path"] for e in data["events"]]
    assert paths == [f"/f{i}" for i in range(100)], "orden por sequence preservado"


# ---------------------------------------------------------------------------
# 2. Slim fallback — nunca perder el evento gigante
# ---------------------------------------------------------------------------

def test_query_byte_cap_single_giant_event_slim_fallback(tmp_path):
    """1 evento 1MB → se conserva (slim) con aviso, no vacío.

    Anti-teatro: una impl que devuelve ``events: []`` cuando el primer
    evento excede el cap pierde el evento y falla (len == 0).
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 1, _make_big_payload)

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    data = json.loads(_text(content_blocks))

    assert len(data["events"]) == 1, "el evento gigante no debe perderse"
    payload = data["events"][0]["event"]["payload"]
    assert "content" not in payload, "degradado a slim → sin contenido pesado"
    assert data["truncated"] is True
    assert data["message"], "aviso de degradación esperado"


# ---------------------------------------------------------------------------
# 3. Cap configurable por env
# ---------------------------------------------------------------------------

def test_query_byte_cap_env_var_configurable(tmp_path, monkeypatch):
    """``CAUSADB_MAX_RESPONSE_BYTES`` chico → el cap respeta el env.

    Anti-teatro: un cap hardcodeado (leído en import-time) ignora el env y
    con 10 eventos chicos (~3KB) no trunca → la aserción de truncated falla.
    """
    monkeypatch.setenv("CAUSADB_MAX_RESPONSE_BYTES", "500")
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 10, lambda i: {
        "path": f"/f{i}", "action": "create", "content": "x" * 100,
    })

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    data = json.loads(_text(content_blocks))

    assert data["truncated"] is True, "cap=500 debe truncar 10 eventos (~3KB)"
    assert data["bytes"] <= 500 + 100, (
        f"bytes={data['bytes']} debe respetar el cap de env (500)+100"
    )


def test_query_byte_cap_boundary_exact():
    """Serialización exactamente en el cap → no trunca (≤ cap ok).

    Anti-teatro: un helper que trunca con ``>=`` en vez de ``>`` corta en
    el boundary y falla.
    """
    from causadb.mcp._tools import _apply_byte_cap

    ev = {
        "event": {
            "event_type": "FILE_MODIFIED",
            "payload": {"path": "/f", "action": "create"},
        },
        "hash": "h",
        "prev_hash": "p",
    }
    serialized = json.dumps(ev, default=str, sort_keys=True)
    kept, cap_info = _apply_byte_cap([ev], len(serialized))
    assert cap_info["truncated"] is False, "exactamente en el cap no trunca"
    assert len(kept) == 1


def test_query_include_payloads_false_never_truncates(tmp_path):
    """Slim (``include_payloads=false``) siempre bajo cap → ``truncated:false``.

    Anti-teatro: una impl que ignora ``include_payloads`` resuelve los 10
    eventos de 1MB (~10MB > cap) y trunca → la aserción falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 10, _make_big_payload)

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
        "include_payloads": False,
    })
    data = json.loads(_text(content_blocks))

    assert data["truncated"] is False, (
        "include_payloads=false debe quedar bajo el cap (fichas slim)"
    )
    assert len(data["events"]) == 10


# ---------------------------------------------------------------------------
# 4. Cap de bytes en revive (C2)
# ---------------------------------------------------------------------------

def test_revive_byte_cap_keeps_newest_decisions(tmp_path, monkeypatch):
    """20 decisiones de reasoning 50KB → revive ≤ cap, conserva las más
    recientes (newest-first), aviso presente.

    Anti-teatro: sin cap el output es ~6KB > cap y la aserción de tamaño
    falla; un trim que recorta desde el PRINCIPIO pierde DECISION-19 y
    falla; sin aviso la aserción de "decisiones omitidas" falla.
    """
    monkeypatch.setenv("CAUSADB_MAX_REVIVE_BYTES", "5000")
    ledger = _make_ledger_with_decisions(tmp_path, _make_decision_specs(20))

    args = _make_revive_args(ledger, "markdown", decisions=20)
    exit_code, output = cmd_revive(args)
    assert exit_code == 0

    cap = int(os.environ["CAUSADB_MAX_REVIVE_BYTES"])
    # Slack 800: la doc base (resume/OCB/score) ~3.4KB + aviso. Sin cap el
    # output sería ~6KB > cap+800 → la aserción discrimina.
    assert len(output) <= cap + 800, (
        f"output={len(output)}B debe ser <= cap({cap})+800 (slack base+aviso)"
    )
    # Conserva las más recientes (la lista es newest-first; el trim va
    # desde el final = las viejas).
    assert "DECISION-19" in output, "la decisión más reciente debe conservarse"
    assert "DECISION-0" not in output, "la decisión más vieja debe omitirse"
    assert "decisiones omitidas" in output, "aviso de omisión esperado"


def test_revive_write_path_not_capped(tmp_path, monkeypatch):
    """``write_path`` → archivo COMPLETO sin truncar; el cap aplica solo al
    return del step 7.

    Anti-teatro: si el cap se aplicara también al write_path, el archivo
    perdería DECISION-0 y la aserción falla.
    """
    monkeypatch.setenv("CAUSADB_MAX_REVIVE_BYTES", "5000")
    ledger = _make_ledger_with_decisions(tmp_path, _make_decision_specs(20))
    out_file = str(tmp_path / "revive.md")

    args = _make_revive_args(ledger, "markdown", write=out_file, decisions=20)
    exit_code, output = cmd_revive(args)
    assert exit_code == 0

    # El return está capado (aviso presente).
    assert "decisiones omitidas" in output

    # El archivo está completo: todas las decisiones, incluidas las viejas.
    with open(out_file) as f:
        file_content = f.read()
    assert "DECISION-0" in file_content, "write_path debe escribir completo"
    assert "DECISION-19" in file_content


def test_revive_byte_cap_json_format(tmp_path, monkeypatch):
    """Output json → key ``truncated_notice`` presente cuando se trunca.

    Anti-teatro: sin cap el JSON completo (~1MB) no lleva truncated_notice
    y la aserción falla.
    """
    monkeypatch.setenv("CAUSADB_MAX_REVIVE_BYTES", "100000")
    ledger = _make_ledger_with_decisions(tmp_path, _make_decision_specs(20))

    args = _make_revive_args(ledger, "json", decisions=20)
    exit_code, output = cmd_revive(args)
    assert exit_code == 0

    data = json.loads(output)
    assert "truncated_notice" in data, "json truncado debe llevar truncated_notice"
    assert "decisiones omitidas" in data["truncated_notice"]
    assert len(data["governance_decisions"]) < 20, (
        "el json debe recortar decisiones para entrar en el cap"
    )
    # Conserva las más recientes (newest-first).
    newest = data["governance_decisions"][0]["reasoning"]
    assert "DECISION-19" in newest


# ---------------------------------------------------------------------------
# 5. Feedback / stream / resource — mismo path de datos, mismo riesgo
# ---------------------------------------------------------------------------

def test_feedback_and_stream_byte_capped(tmp_path):
    """Feedback/stream: forma preservada (array) bajo el cap; dict marcador
    cuando el cap de bytes se dispara.

    Anti-teatro: sin cap, 10 eventos de 100KB (~1MB) se devuelven completos
    como array y la aserción de dict/truncated falla.
    """
    # Caso chico → array (forma preservada, no rompe tests existentes).
    small = str(tmp_path / "small.log")
    _write_events(small, 2, lambda i: {"feedback": f"fb{i}"},
                  event_type=EventType.HUMAN_FEEDBACK)
    server = create_server()
    content_blocks, _ = _call_tool(server, "feedback", {"ledger_path": small})
    small_result = json.loads(_text(content_blocks))
    assert isinstance(small_result, list), "ledger chico → array"

    # Caso grande → truncado con marcador.
    big = str(tmp_path / "big.log")
    _write_events(big, 10, lambda i: {"feedback": "x" * 100000, "idx": i},
                  event_type=EventType.HUMAN_FEEDBACK)
    content_blocks, _ = _call_tool(server, "feedback", {"ledger_path": big})
    big_result = json.loads(_text(content_blocks))
    assert isinstance(big_result, dict), "cap activado → dict con marcador"
    assert big_result["truncated"] is True
    assert 0 < len(big_result["events"]) < 10
    assert big_result["message"]

    # Stream: mismo comportamiento.
    sbig = str(tmp_path / "sbig.log")
    _write_events(sbig, 10, lambda i: {"reason": "x" * 100000, "idx": i},
                  event_type=EventType.STREAM_INTERRUPTED)
    content_blocks, _ = _call_tool(server, "stream", {"ledger_path": sbig})
    sbig_result = json.loads(_text(content_blocks))
    assert isinstance(sbig_result, dict)
    assert sbig_result["truncated"] is True
    assert 0 < len(sbig_result["events"]) < 10


def test_events_resource_byte_capped(tmp_path):
    """Resource ``causadb://events`` con eventos grandes → truncado con
    marcador ``{"truncated": true, "events": [...], "message": ...}``.

    Anti-teatro: sin cap el resource devuelve los 5MB completos como array
    y la aserción de dict/truncated falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _write_events(ledger_path, 5, _make_big_payload)

    server = create_server(config_ledger_path=ledger_path)

    async def _read():
        contents = await server.read_resource("causadb://events")
        return contents[0].content
    text = anyio.run(_read)
    data = json.loads(text)

    assert isinstance(data, dict), "cap activado → dict con marcador"
    assert data["truncated"] is True
    assert len(data["events"]) >= 1, "nunca vacío (slim fallback)"
    assert data["message"]