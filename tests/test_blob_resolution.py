import json
import os
from types import MappingProxyType
from unittest.mock import patch

import pytest

from causadb._blob_store import BlobStore, resolve_payload
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_index import LedgerIndex
from causadb._ledger_writer import LedgerWriter
from causadb._query_engine import query_events
from causadb._replay_engine import ReplayEngine


def _blob_ledger(tmp_path, event_type, payload):
    """Crear un workspace con un evento cuyo payload fue externalizado a blob."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
    writer = LedgerWriter(ledger, config=config)
    event = CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(payload),
    )
    writer.append(event)
    return ledger, config, event


def _last_entry(ledger):
    with open(ledger) as f:
        lines = [l for l in f if l.strip()]
    return json.loads(lines[-1])


class TestResolvePayloadHelper:
    def test_resolves_blob_key(self, tmp_path):
        store = BlobStore(base_path=str(tmp_path / "blobs"))
        blob_hash = store.put({"reasoning": "texto del blob", "impact": "high"})
        result = resolve_payload({"$blob": blob_hash}, store)
        assert result == {"reasoning": "texto del blob", "impact": "high"}

    def test_key_based_check_ignores_literal_value(self, tmp_path):
        store = BlobStore(base_path=str(tmp_path / "blobs"))
        result = resolve_payload({"content": "$blob"}, store)
        assert result == {"content": "$blob"}

    def test_none_store_passthrough(self, tmp_path):
        store = BlobStore(base_path=str(tmp_path / "blobs"))
        blob_hash = store.put({"reasoning": "x"})
        assert resolve_payload({"$blob": blob_hash}, None) == {"$blob": blob_hash}

    def test_missing_blob_raises_file_not_found(self, tmp_path):
        """(BIT-CHR.35 P2) Un ``$blob`` a un hash inexistente debe lanzar
        ``FileNotFoundError`` descriptivo en vez de devolver ``{}`` silencioso."""
        store = BlobStore(base_path=str(tmp_path / "blobs"))
        missing_hash = "a" * 64
        with pytest.raises(FileNotFoundError) as exc_info:
            resolve_payload({"$blob": missing_hash}, store)
        msg = str(exc_info.value)
        # El mensaje debe incluir el hash del blob faltante...
        assert missing_hash in msg
        # ...la ruta derivada del blob (base_path + shard + hash.json)...
        expected_blob_path = os.path.join(
            store.base_path, missing_hash[:2], missing_hash[2:4], missing_hash + ".json"
        )
        assert expected_blob_path in msg
        # ...el directorio del ledger (derivado de blob_store.base_path)...
        expected_ledger_dir = os.path.dirname(store.base_path)
        assert expected_ledger_dir in msg
        # ...y ser descriptivo (mencionar "blob").
        assert "blob" in msg.lower()

    def test_plain_payload_passthrough(self, tmp_path):
        store = BlobStore(base_path=str(tmp_path / "blobs"))
        payload = {"reasoning": "inline"}
        assert resolve_payload(payload, store) is payload


class TestReplayResolvesBlob:
    def test_governance_decision_reasoning_resolved(self, tmp_path):
        reasoning = "Aprobar migración a particionado por fecha " + "A" * 1500
        payload = {
            "reasoning": reasoning,
            "impact": "high",
            "decision_type": "tactical",
            "origin": "agent",
            "confidence": 0.95,
        }
        ledger, config, event = _blob_ledger(
            tmp_path, EventType.GOVERNANCE_DECISION, payload
        )
        entry = _last_entry(ledger)
        assert set(entry["event"]["payload"]) == {"$blob"}

        state = ReplayEngine(ledger).reconstruct_state()
        gd = [g for g in state["governance_decisions"] if g.get("event_id") == event.event_id]
        assert len(gd) == 1
        assert gd[0]["reasoning"] == reasoning
        assert gd[0]["impact"] == "high"
        assert gd[0]["decision_type"] == "tactical"


class TestReviveMarkdownWithBlob:
    def test_revive_markdown_does_not_crash(self, tmp_path):
        from causadb.cli._cmd_revive import _run_revive

        reasoning = "Decisión con razonamiento externalizado a blob " + "B" * 1500
        payload = {
            "reasoning": reasoning,
            "impact": "high",
            "decision_type": "tactical",
            "origin": "agent",
        }
        ledger, config, event = _blob_ledger(
            tmp_path, EventType.GOVERNANCE_DECISION, payload
        )
        rc, out = _run_revive(ledger_path=ledger, output_format="markdown", max_decisions=10)
        assert rc == 0, f"revive failed: {out[:500]}"
        assert reasoning[:50] in out


class TestQueryFindsBlobContent:
    def test_query_events_text_finds_blob_content(self, tmp_path):
        marker = "UNIQUE_MARKER_7f9a2c"
        payload = {
            "path": "/tmp/blobbed.txt",
            "action": "create",
            "notes": marker + "x" * 2000,
        }
        ledger, config, event = _blob_ledger(
            tmp_path, EventType.FILE_MODIFIED, payload
        )
        entry = _last_entry(ledger)
        assert set(entry["event"]["payload"]) == {"$blob"}

        results = query_events(ledger, text=marker)
        assert len(results) == 1
        assert results[0]["payload"]["notes"].startswith(marker)

    def test_ledger_index_query_resolves_payload(self, tmp_path):
        reasoning = "Index debe resolver payload externalizado " + "C" * 1500
        payload = {
            "reasoning": reasoning,
            "impact": "medium",
            "decision_type": "strategic",
            "origin": "agent",
        }
        ledger, config, event = _blob_ledger(
            tmp_path, EventType.GOVERNANCE_DECISION, payload
        )
        index = LedgerIndex(ledger)

        fast = index.query(event_type="GOVERNANCE_DECISION")
        assert len(fast) == 1
        assert fast[0]["event"]["payload"]["reasoning"] == reasoning

        delegated = index.query(event_type="GOVERNANCE_DECISION", text=reasoning[:30])
        assert len(delegated) == 1
        assert delegated[0]["event"]["payload"]["reasoning"] == reasoning


class TestMissingBlobFailFast:
    """(BIT-CHR.35 P2) Un blob faltante debe hacer fallar el replay/lectura
    con un mensaje descriptivo, NO degradar el estado silenciosamente."""

    def _write_event_with_fake_blob(self, tmp_path, payload, fake_hash):
        """Escribe un evento cuyo payload fue "externalizado" a un hash falso
        (el blob nunca se materializa en disco)."""
        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]
        config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
        writer = LedgerWriter(ledger, config=config)
        event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType(payload),
        )
        with patch("causadb._blob_store.BlobStore.put", return_value=fake_hash):
            writer.append(event)
        return ledger, config, event, fake_hash

    def test_reconstruct_state_fails_with_descriptive_error(self, tmp_path):
        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "decisión grande " + "x" * 2000},
            "a" * 64,
        )
        blob_path = os.path.join(
            config.blob_store_path, fake_hash[:2], fake_hash[2:4], fake_hash + ".json"
        )
        assert not os.path.exists(blob_path)

        with open(ledger) as f:
            before = f.read()

        # reconstruct_state debe fallar (no devolver estado degradado).
        with pytest.raises(FileNotFoundError) as exc_info:
            ReplayEngine(ledger).reconstruct_state()
        msg = str(exc_info.value)
        # El mensaje debe incluir el hash del blob faltante y la ruta derivada.
        assert fake_hash in msg, f"hash {fake_hash!r} not in error message: {msg!r}"
        assert blob_path in msg, f"blob path {blob_path!r} not in error message: {msg!r}"
        # Y el directorio del ledger (derivado del blob_store.base_path) para
        # diagnóstico del operador. ``resolve_payload`` no recibe la ruta exacta
        # del ledger.log (firma intacta), pero sí puede derivar su directorio
        # padre desde ``blob_store.base_path`` (= dirname(ledger)/blobs).
        ledger_dir = os.path.dirname(ledger)
        assert ledger_dir in msg, f"ledger dir {ledger_dir!r} not in error message: {msg!r}"

        # El ledger NO debe mutarse (fail-fast no escribe).
        with open(ledger) as f:
            after = f.read()
        assert after == before

    def test_read_all_entries_fails_on_missing_blob(self, tmp_path):
        """Los generators explotan en iteración: ``list(...)`` debe forzar el raise."""
        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "payload externalizado " + "y" * 2000},
            "c" * 64,
        )
        from causadb._ledger_reader import LedgerReader

        with pytest.raises(FileNotFoundError) as exc_info:
            list(LedgerReader(ledger).read_all_entries())
        msg = str(exc_info.value)
        assert fake_hash in msg
        assert "blob" in msg.lower()

    def test_read_all_fails_on_missing_blob(self, tmp_path):
        """``read_all`` (que envuelve ``read_all_entries``) también debe explotar."""
        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "payload externalizado " + "z" * 2000},
            "d" * 64,
        )
        from causadb._ledger_reader import LedgerReader

        with pytest.raises(FileNotFoundError):
            list(LedgerReader(ledger).read_all())

    def test_ledger_index_query_fails_on_missing_blob(self, tmp_path):
        """``LedgerIndex.query`` resuelve blobs vía ``resolve_payload``; debe fallar."""
        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "index payload " + "w" * 2000},
            "e" * 64,
        )
        index = LedgerIndex(ledger)
        with pytest.raises(FileNotFoundError) as exc_info:
            index.query(event_type="GOVERNANCE_DECISION")
        msg = str(exc_info.value)
        assert fake_hash in msg

    def test_revive_degrades_with_banner_on_missing_blob(self, tmp_path):
        """R4 — Política blob faltante (GOV linked): revive CONTINÚA (rc==0)
        con banner prominente al tope del markdown. El hash falso debe
        aparecer en el output (reutilizado del mensaje descriptivo de
        ``resolve_payload``)."""
        from causadb.cli._cmd_revive import _run_revive

        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "revive payload " + "v" * 2000},
            "f" * 64,
        )
        rc, out = _run_revive(ledger_path=ledger, output_format="markdown", max_decisions=10)
        assert rc == 0, (
            f"revive debe degradar (rc=0) ante blob faltante, no fallar: "
            f"rc={rc}, out={out[:500]}"
        )
        assert "BLOBS FALTANTES" in out, f"sin banner: {out[:500]!r}"
        assert fake_hash in out, f"hash {fake_hash!r} not in revive output: {out[:500]!r}"
        assert "> Primer error:" in out

    def test_revive_json_degraded_flag_on_missing_blob(self, tmp_path):
        """R4 — En JSON, blob faltante ⇒ campo estructurado ``degraded`` +
        ``degraded_detail`` (sin texto de banner), rc==0."""
        from causadb.cli._cmd_revive import _run_revive

        ledger, config, event, fake_hash = self._write_event_with_fake_blob(
            tmp_path,
            {"reasoning": "revive payload json " + "j" * 2000},
            "b" * 64,
        )
        rc, out = _run_revive(ledger_path=ledger, output_format="json", max_decisions=10)
        assert rc == 0, (
            f"revive debe degradar (rc=0) ante blob faltante, no fallar: "
            f"rc={rc}, out={out[:500]}"
        )
        data = json.loads(out)
        assert data["degraded"] is True
        assert data["degraded_detail"]["error_count"] >= 1
        assert fake_hash in json.dumps(data["degraded_detail"]["errors"]), (
            f"hash {fake_hash!r} not in degraded_detail errors"
        )


class TestReviveHealthyBlobNoBanner:
    """R4 anti-teatro: con blobs SANOS no hay banner ni flag ``degraded``.
    Mata un banner hardcodeado o un flag siempre-True."""

    def test_revive_healthy_blob_no_banner(self, tmp_path):
        from causadb.cli._cmd_revive import _run_revive

        reasoning = "Decisión sana externalizada a blob " + "S" * 1500
        payload = {
            "reasoning": reasoning,
            "impact": "high",
            "decision_type": "tactical",
            "origin": "agent",
        }
        ledger, config, event = _blob_ledger(
            tmp_path, EventType.GOVERNANCE_DECISION, payload
        )
        rc_md, out_md = _run_revive(
            ledger_path=ledger, output_format="markdown", max_decisions=10
        )
        assert rc_md == 0, f"out={out_md[:500]}"
        assert "BLOBS FALTANTES" not in out_md

        rc_json, out_json = _run_revive(
            ledger_path=ledger, output_format="json", max_decisions=10
        )
        assert rc_json == 0, f"out={out_json[:500]}"
        data = json.loads(out_json)
        assert data["degraded"] is False
        assert "degraded_detail" not in data


class TestNonBlobErrorNotDegraded:
    """R4 guard falso-positivo: un error DURO (no-blob) NO marca ``degraded``.

    Mata el anti-patrón "except genérico marca el flag" y el orden invertido
    de catches (``except Exception`` antes de ``except BlobNotFoundError``).
    """

    def test_non_blob_error_does_not_mark_degraded(self, tmp_path):
        from causadb.cli._cmd_revive import _run_revive

        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]

        # compute_score se importa DENTRO de _run_revive vía
        # ``from causadb._score import compute_score`` ⇒ el patch target es
        # el módulo de origen, no el CLI.
        with patch("causadb._score.compute_score", side_effect=ValueError("boom")):
            rc, out = _run_revive(
                ledger_path=ledger, output_format="json", max_decisions=10
            )
        assert rc == 0, f"error duro de score no debe matar revive: out={out[:500]}"
        data = json.loads(out)
        assert data["degraded"] is False, (
            f"ValueError (no-blob) NO debe marcar degraded: "
            f"{data.get('degraded_detail')}"
        )
        assert "degraded_detail" not in data


class TestInlinePayloadsStillWork:
    """(BIT-CHR.35 P2) Regresión: los payloads inline (sin ``$blob``) siguen OK."""

    def test_reconstruct_state_with_inline_payload(self, tmp_path):
        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]
        config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
        writer = LedgerWriter(ledger, config=config)
        event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({"reasoning": "inline corto", "impact": "low"}),
        )
        writer.append(event)

        state = ReplayEngine(ledger).reconstruct_state()
        gd = [g for g in state["governance_decisions"] if g.get("event_id") == event.event_id]
        assert len(gd) == 1
        assert gd[0]["reasoning"] == "inline corto"
        assert gd[0]["impact"] == "low"
