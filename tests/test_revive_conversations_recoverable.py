"""C.3 — Tarjeta "Conversaciones recuperables" en revive.

Sesiones harvesteadas con `conversation_ref` (contrato C.2) listadas en
revive con tool/session/locator/estado + puntero a `causadb recover`.
Sin resolver blobs ni leer fuentes (Art. V): usa el state de ReplayEngine
ya computado. Disjunta de "Sesiones Recientes" (MAJOR-7).
"""

from types import MappingProxyType
import argparse

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import cmd_revive


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_revive_args(ledger, fmt="markdown"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    return parser.parse_args(["--ledger", ledger, "--format", fmt])


def _append(writer, event_type, payload, source="causadb:test"):
    writer.append(CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source=source,
        payload=MappingProxyType(payload),
    ))


def _section(output, title):
    """Devuelve el contenido de una sección '## <title>' hasta el siguiente
    header '## ' (para no contaminar con secciones posteriores)."""
    marker = f"## {title}"
    if marker not in output:
        return ""
    rest = output.split(marker, 1)[1]
    lines = rest.splitlines()
    for i, line in enumerate(lines):
        if i > 0 and line.startswith("## "):
            return "\n".join(lines[:i])
    return rest


# ── tests ────────────────────────────────────────────────────────────────────


def test_replay_conserva_conversation_ref_en_state(tmp_path):
    """C.3 — ReplayEngine conserva conversation_ref en state, deduplicado por
    session_id (varios eventos de la misma sesión → una sola entrada)."""
    from causadb._replay_engine import ReplayEngine
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    ref = {"provider": "opencode", "native_id": "s-1",
           "locator_kind": "sqlite", "locator": "opencode_default",
           "resolver": "opencode", "confidence": "verified",
           "content_class": "transcript_complete", "privacy_class": "raw_sensitive"}

    _append(writer, "TOOL_CALLED", {"session_id": "s-1", "conversation_ref": ref,
                                    "tool_name": "bash", "result": "ok"})
    _append(writer, "TOOL_CALLED", {"session_id": "s-1", "conversation_ref": ref,
                                    "tool_name": "grep", "result": "x"})
    _append(writer, "TOOL_CALLED", {"session_id": "s-2", "conversation_ref": ref,
                                    "tool_name": "read", "result": "y"})
    _append(writer, "FILE_MODIFIED", {"path": "/a", "action": "create"})

    state = ReplayEngine(ledger).reconstruct_state()
    convs = state.get("conversations_recoverable", {})
    assert set(convs.keys()) == {"s-1", "s-2"}
    assert convs["s-1"]["last_event_type"] == "TOOL_CALLED"
    assert convs["s-1"]["conversation_ref"]["provider"] == "opencode"


def test_revive_tarjeta_conversaciones_recoverables(tmp_path):
    """C.3 — revive (markdown) renderiza la tarjeta con provider, session,
    locator y estado, más el puntero a `causadb recover`."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    ref = {"provider": "opencode", "native_id": "s-9",
           "locator_kind": "sqlite", "locator": "opencode_default",
           "resolver": "opencode", "confidence": "verified",
           "content_class": "transcript_complete", "privacy_class": "raw_sensitive"}
    _append(writer, "LLM_INVOKED", {"session_id": "s-9", "conversation_ref": ref,
                                    "provider": "opencode", "model": "m"})

    code, out = cmd_revive(_make_revive_args(ledger))
    assert code == 0
    section = _section(out, "Conversaciones recuperables")
    assert "causadb recover" in section
    assert "s-9" in section
    assert "opencode" in section
    assert "sqlite/opencode_default" in section


def test_revive_sin_conversation_ref_omite_tarjeta(tmp_path):
    """C.3 — sin eventos con conversation_ref, la tarjeta no se renderiza."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    _append(writer, "TOOL_CALLED", {"tool_name": "bash", "result": "ok"})

    code, out = cmd_revive(_make_revive_args(ledger))
    assert code == 0
    assert "## Conversaciones recuperables" not in out


def test_revive_json_incluye_conversations_recoverable(tmp_path):
    """C.3 — el output JSON de revive expone conversations_recoverable."""
    import json
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    ref = {"provider": "gemini", "native_id": "g-1",
           "locator_kind": "sqlite", "locator": "gemini_default",
           "resolver": "gemini", "confidence": "verified",
           "content_class": "transcript_complete", "privacy_class": "raw_sensitive"}
    _append(writer, "TOOL_CALLED", {"session_id": "g-1", "conversation_ref": ref,
                                    "tool_name": "bash", "result": "ok"})

    code, out = cmd_revive(_make_revive_args(ledger, fmt="json"))
    assert code == 0
    data = json.loads(out)
    convs = data.get("conversations_recoverable", {})
    assert "g-1" in convs
    assert convs["g-1"]["conversation_ref"]["provider"] == "gemini"
