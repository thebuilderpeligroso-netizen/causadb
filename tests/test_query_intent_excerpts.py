"""Q.1 + Q.2 — intent_only (excluir REASONING_STEP/TOOL_CALLED en texto) y
excerpts de match. Tests RED primero (Art. III), mutación discriminatoria (Art. IX).
"""

from types import MappingProxyType

import json

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._init import causadb_init
from causadb._query_engine import query_events
from causadb._ledger_index import LedgerIndex


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger_noise(tmp_path):
    """Ledger con REASONING_STEP + TOOL_CALLED (ruido) y FILE_MODIFIED (intención),
    todos conteniendo el needle "needle" en su payload."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    noise_rs = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="harvester:opencode",
        source="harvester:opencode",
        timestamp="2026-06-01T00:00:00Z",
        payload=MappingProxyType({
            "step_type": "reasoning",
            "description": "thinking about needle in reasoning",
        }),
    )
    noise_tool = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="harvester:opencode",
        source="harvester:opencode",
        timestamp="2026-06-02T00:00:00Z",
        payload=MappingProxyType({
            "tool_name": "bash",
            "arguments": {"command": "echo needle tool"},
        }),
    )
    intent_file = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-alpha",
        source="causadb:vigilante",
        timestamp="2026-06-03T00:00:00Z",
        payload=MappingProxyType({"path": "/rna/needle_results.txt", "action": "create"}),
    )
    writer.append(noise_rs)
    writer.append(noise_tool)
    writer.append(intent_file)

    return ledger


@pytest.fixture
def ledger_big_payload(tmp_path):
    """Ledger con un FILE_MODIFIED cuyo payload contiene 'needle' rodeado de contexto
    largo (para probar excerpts con contexto adyacente)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    context = "x_" * 200
    big = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-alpha",
        source="test",
        timestamp="2026-06-01T00:00:00Z",
        payload=MappingProxyType({
            "path": f"/rna/{context}needle{context}.txt",
            "action": "create",
        }),
    )
    writer.append(big)

    return ledger


# ── Q.1 — intent_only ─────────────────────────────────────────────────────────


class TestIntentOnlyTextFilter:
    def test_query_text_excludes_reasoning_tool_by_default(self, ledger_noise):
        """Por defecto, text= excluye REASONING_STEP y TOOL_CALLED (solo intención)."""
        results = query_events(ledger_noise, text="needle")
        types = {e["event_type"] for e in results}
        assert "REASONING_STEP" not in types
        assert "TOOL_CALLED" not in types
        assert "FILE_MODIFIED" in types
        assert len(results) == 1

    def test_query_text_intent_only_false_includes_noise(self, ledger_noise):
        """Con intent_only=False se recupera el ruido también (flag discriminatoria)."""
        results = query_events(ledger_noise, text="needle", intent_only=False)
        types = {e["event_type"] for e in results}
        assert "REASONING_STEP" in types
        assert "TOOL_CALLED" in types
        assert "FILE_MODIFIED" in types
        assert len(results) == 3

    def test_query_text_with_explicit_event_type_wins(self, ledger_noise):
        """Guard GREEN: event_type explícito gana sobre la exclusión por defecto."""
        results = query_events(
            ledger_noise, event_type="REASONING_STEP", text="needle"
        )
        assert len(results) == 1
        assert results[0]["event_type"] == "REASONING_STEP"

    def test_ledger_index_respects_intent_only(self, ledger_noise):
        """El path MCP (LedgerIndex.query) respeta intent_only en texto."""
        index = LedgerIndex(ledger_noise)
        results = index.query(text="needle")
        types = {e["event"]["event_type"] for e in results}
        assert "REASONING_STEP" not in types
        assert "TOOL_CALLED" not in types
        assert "FILE_MODIFIED" in types

    def test_query_text_excludes_noise_without_resolving_blobs(self, ledger_noise, monkeypatch):
        """Anti-teatro (Art. V): la exclusión ocurre ANTES de resolver blobs.
        Ningún payload de REASONING_STEP/TOOL_CALLED debe pasar por resolve_payload
        (el genesis SYSTEM_BOOT sí se resuelve por ser candidato legítimo)."""
        import causadb._query_engine as qe
        original = qe.resolve_payload
        resolved = []

        def spy(payload, blob_store):
            resolved.append(payload)
            return original(payload, blob_store)

        monkeypatch.setattr(qe, "resolve_payload", spy)
        query_events(ledger_noise, text="needle")
        dumped = [json.dumps(p, sort_keys=True) for p in resolved]
        assert not any("thinking about needle" in d for d in dumped), (
            "El payload del REASONING_STEP fue resuelto; intent_only debe excluirlo "
            "ANTES de tocar blob storage."
        )
        assert not any("echo needle tool" in d for d in dumped), (
            "El payload del TOOL_CALLED fue resuelto; intent_only debe excluirlo "
            "ANTES de tocar blob storage."
        )
        assert any("needle_results.txt" in d for d in dumped), (
            "El FILE_MODIFIED (intención) sí debe resolverse."
        )


# ── Q.2 — excerpts ────────────────────────────────────────────────────────────


class TestQueryExcerpts:
    def test_query_text_excerpt_includes_match(self, ledger_big_payload):
        """include_excerpts=True → el resultado lleva 'excerpt' con el needle."""
        results = query_events(
            ledger_big_payload, text="needle", include_excerpts=True
        )
        assert len(results) == 1
        assert "excerpt" in results[0]
        assert "needle" in results[0]["excerpt"]

    def test_query_text_excerpt_has_context_around_match(self, ledger_big_payload):
        """Anti-teatro (Art. IX): el excerpt debe tener contexto adyacente al needle,
        no ser solo el needle (una implementación degenerada excerpt=needle falla)."""
        results = query_events(
            ledger_big_payload, text="needle", include_excerpts=True
        )
        assert len(results) == 1
        excerpt = results[0]["excerpt"]
        assert len(excerpt) > len("needle")
        assert "x_" in excerpt or "_x" in excerpt

    def test_query_text_no_excerpt_by_default(self, ledger_big_payload):
        """Sin include_excerpts → no hay key 'excerpt' (backward compat)."""
        results = query_events(ledger_big_payload, text="needle")
        assert len(results) == 1
        assert "excerpt" not in results[0]

    def test_ledger_index_excerpt_reaches_mcp_path(self, ledger_big_payload):
        """El path MCP (LedgerIndex.query) devuelve excerpts — NO los descarta.
        (Hallazgo Checker: LedgerIndex re-lee el ledger desde raw lines y perdía
        el excerpt de query_events.)"""
        index = LedgerIndex(ledger_big_payload)
        results = index.query(text="needle", include_excerpts=True)
        assert len(results) == 1
        entry = results[0]
        assert "excerpt" in entry["event"]
        assert "needle" in entry["event"]["excerpt"]

    def test_query_text_excerpt_is_bounded(self, ledger_big_payload):
        """MENOR-Checker: el excerpt tiene tamaño acotado (~2*120 + needle + elipsis).
        Mutación 'excerpt = payload completo' FALLA este test."""
        results = query_events(
            ledger_big_payload, text="needle", include_excerpts=True
        )
        assert len(results) == 1
        excerpt = results[0]["excerpt"]
        # payload del fixture: ~400 chars de contexto de cada lado.
        assert len(excerpt) <= 2 * 120 + len("needle") + 6
        assert "x_" in excerpt or "_x" in excerpt
