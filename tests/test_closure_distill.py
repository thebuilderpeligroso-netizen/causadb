"""E-Causal — Tests for Passive Session Closure Distillation and Concurrency Endorsements.

Verifica:
1. Destilación pasiva automática a partir de un SESSION_SUMMARY en revive.
2. Evitación de duplicados mediante el registro de CONTEXT_UPDATED como endoso de concurrencia.
"""

import json
import os
import shutil
import time
import uuid
import pytest
from datetime import datetime

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._config import CausaDBConfig
from causadb._ledger_writer import LedgerWriter
from causadb._ledger_reader import LedgerReader
from causadb._decision_distill import distill_closure_decision
from causadb.cli._cmd_revive import _run_revive

# Ledger real de producción (el test de perf opera sobre una COPIA en
# tmp — el ledger real solo se LEE para localizar la sesión de prueba).
LEDGER_REAL = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/.causadb/ledger.log"

# Umbral perf: doble read_all() mide ~5.1s; implementación indexada ~2.5s frío.
PERF_THRESHOLD_SECONDS = 4.0

@pytest.fixture
def temp_ledger(tmp_path):
    ledger_file = tmp_path / "test_ledger.log"
    # Registrar genesis de prueba
    config = CausaDBConfig(ledger_path=str(ledger_file))
    writer = LedgerWriter(str(ledger_file), config)
    writer.append(CanonicalEvent.from_dict({
        "event_id": "genesis-event-12345",
        "event_type": "SYSTEM_BOOT",
        "timestamp": datetime.now().isoformat(),
        "source": "causadb:init",
        "ctx_id": "genesis",
        "payload": {"version": "1.0.0"}
    }))
    return str(ledger_file)


def test_passive_closure_distillation_success(temp_ledger, monkeypatch):
    """Test RED 1: Al correr revive en un abrupt_close con SESSION_SUMMARY,
    se destila automáticamente la GOVERNANCE_DECISION de cierre sin duplicarla
    si ya existe.
    """
    ledger_path = temp_ledger
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    # 1. Escribir un SESSION_SUMMARY huérfano (sesión abrupta)
    import uuid
    target_session_id = "test-abrupt-session-123"
    summary_event_id = str(uuid.uuid4())
    writer.append(CanonicalEvent.from_dict({
        "event_id": summary_event_id,
        "event_type": EventType.SESSION_SUMMARY.value,
        "timestamp": datetime.now().isoformat(),
        "source": "harvester:opencode",
        "ctx_id": "test-ctx",
        "payload": {
            "__harvest_session_id": target_session_id, # FIX: Usar el campo que usa el summarizer
            "session_id": target_session_id, # Mantener ambos por seguridad
            "tool": "opencode",
            "turn_count": 5,
            "summary_lines": [
                "user: fix tests",
                "assistant: I modified calculator.py"
            ],
            "errors": []
        }
    }))

    # Simular que el resume indica abrupt_close para esa sesión.
    def mock_generate_resume(ledger_path, state=None):
        return {
            "last_session_id": target_session_id,
            "session_type": "abrupt_close"
        }

    monkeypatch.setattr("causadb.cli._cmd_resume.generate_resume", mock_generate_resume)

    # Corremos el revive pipeline interno
    exit_code, output = _run_revive(
        ledger_path=ledger_path,
        output_format="markdown",
        max_decisions=5
    )

    assert exit_code == 0

    # 2. El lector debe contener la GOVERNANCE_DECISION destilada automáticamente
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())
    
    gov_decisions = [
        ev for ev in events 
        if ev.event_type == EventType.GOVERNANCE_DECISION
        and ev.payload.get("origin") == "distill"
    ]

    assert len(gov_decisions) == 1, "Debería haberse destilado exactamente un evento de cierre"
    assert gov_decisions[0].parent_event_id == summary_event_id, "Debe enlazarse al summary original"
    assert "Cierre de sesión automático (distilado pasivo)" in gov_decisions[0].payload.get("reasoning")


def test_passive_closure_concurrency_endorsement(temp_ledger, monkeypatch):
    """Test RED 2: Si otro agente/proceso intenta correr revive y ya existe
    la decisión de cierre, registra un CONTEXT_UPDATED de endoso en lugar de duplicarla.
    """
    ledger_path = temp_ledger
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    import uuid
    target_session_id = "test-concurrent-session-999"
    summary_id = str(uuid.uuid4())

    # Escribir el SESSION_SUMMARY
    writer.append(CanonicalEvent.from_dict({
        "event_id": summary_id,
        "event_type": EventType.SESSION_SUMMARY.value,
        "timestamp": datetime.now().isoformat(),
        "source": "harvester:opencode",
        "ctx_id": "concurrent-ctx",
        "payload": {
            "session_id": target_session_id,
            "tool": "opencode",
            "turn_count": 2,
            "summary_lines": ["user: test concurrency"],
            "errors": []
        }
    }))

    # Mock generate_resume to return the expected session info
    def mock_generate_resume(ledger_path, state=None):
        return {
            "last_session_id": target_session_id,
            "session_type": "abrupt_close"
        }

    monkeypatch.setattr("causadb.cli._cmd_resume.generate_resume", mock_generate_resume)

    # Ejecución 1: Crea la decisión de cierre oficial
    exit_code_1, _ = _run_revive(ledger_path=temp_ledger, output_format="markdown")
    assert exit_code_1 == 0

    # Ejecución 2: Intento concurrente por otro contexto/agente.
    # Simulamos cambio de variable para identificar que es otro actor
    os.environ["CAUSADB_AGENT"] = "gemini-cli"
    
    exit_code_2, _ = _run_revive(ledger_path=temp_ledger, output_format="markdown")
    assert exit_code_2 == 0

    # Limpiar env
    os.environ.pop("CAUSADB_AGENT", None)

    reader = LedgerReader(temp_ledger)
    events = list(reader.read_all())

    # Aserciones de duplicación y endoso
    gov_decisions = [
        ev for ev in events 
        if ev.event_type == EventType.GOVERNANCE_DECISION
        and ev.payload.get("origin") == "distill"
    ]
    # No debe duplicarse la decisión de gobernanza
    assert len(gov_decisions) == 1, "La gobernanza de cierre no debe duplicarse"

    # Debe haberse registrado un CONTEXT_UPDATED de endoso indicando la concurrencia
    endorsements = [
        ev for ev in events
        if ev.event_type == EventType.CONTEXT_UPDATED
        and ev.payload.get("action") == "distillation_endorsement"
    ]
    assert len(endorsements) == 1, "Debería registrarse un endoso de distilación por concurrencia"
    assert endorsements[0].payload.get("target_session_id") == target_session_id
    assert endorsements[0].payload.get("original_decision_id") == gov_decisions[0].event_id


# ---------------------------------------------------------------------------
# Fase 3 (perf) — distill_closure_decision via LedgerIndex.query
# ---------------------------------------------------------------------------


def test_distill_closure_no_warning_spam(temp_ledger, caplog):
    """Anti-spam: distill_closure_decision NO debe emitir warnings 'DEBUG:'
    por evento (el código viejo logueaba ~2 líneas por evento → ~340K en
    el ledger real).
    """
    import logging as _logging

    config = CausaDBConfig(ledger_path=temp_ledger)
    writer = LedgerWriter(temp_ledger, config)
    # Un summary + un evento más para ejercitar el loop de matching.
    writer.append(CanonicalEvent.from_dict({
        "event_id": str(uuid.uuid4()),
        "event_type": EventType.SESSION_SUMMARY.value,
        "timestamp": datetime.now().isoformat(),
        "source": "harvester:opencode",
        "ctx_id": "spam-ctx",
        "payload": {
            "session_id": "spam-sess",
            "tool": "opencode",
            "turn_count": 1,
            "summary_lines": [],
            "errors": [],
        },
    }))

    with caplog.at_level(_logging.WARNING):
        result = distill_closure_decision(temp_ledger, "session-inexistente")

    assert result is None
    spam = [r for r in caplog.records if r.getMessage().startswith("DEBUG:")]
    assert not spam, f"{len(spam)} registros DEBUG emitidos (deberían ser 0)"


def test_distill_closure_performance_real_ledger(tmp_path):
    """Perf contra una COPIA del ledger REAL ejercitando las DOS pasadas.

    Diseño (sin contaminar producción):
      - Se copian ledger + índice + last_hash a tmp y los blobs se
        enlazan por symlink (solo lectura).
      - Se usa una sesión REAL que ya tiene SESSION_SUMMARY +
        GOVERNANCE_DECISION → la función recorre pasada 1 (buscar
        summary) Y pasada 2 (buscar decisión existente), que es el
        costo dominante del timeout MCP de revive (30s).
      - El write del endoso cae en la COPIA, nunca en el ledger real.

    Umbral 4.0s: código viejo (doble read_all) mide ~5.1s en esta
    máquina; código indexado mide ~2.5s frío.
    """
    if not os.path.exists(LEDGER_REAL):
        pytest.skip("ledger real no disponible en este entorno")

    # --- Setup: localizar sesión real cerrada (summary + decisión) ---
    from causadb._ledger_index import LedgerIndex

    real_dir = os.path.dirname(LEDGER_REAL)
    index = LedgerIndex(LEDGER_REAL)
    decision_parents = {
        entry["event"].get("parent_event_id")
        for entry in index.query(
            event_type=EventType.GOVERNANCE_DECISION.value,
            limit=None,
            include_payloads=False,
        )
        if entry["event"].get("parent_event_id")
    }
    closed_session_id = None
    for entry in index.query(
        event_type=EventType.SESSION_SUMMARY.value,
        limit=None,
        include_payloads=True,
    ):
        event = entry["event"]
        if event.get("event_id") not in decision_parents:
            continue
        payload = event.get("payload") or {}
        sid = payload.get("session_id") or payload.get("__harvest_session_id")
        if sid:
            closed_session_id = str(sid)
            break
    if closed_session_id is None:
        pytest.skip("no hay sesión con summary+decisión en el ledger real")

    # --- Copia aislada del ledger (ledger + índice + last_hash + blobs→symlink) ---
    workdir = tmp_path / "causadb-copy"
    workdir.mkdir()
    shutil.copy2(LEDGER_REAL, workdir / "ledger.log")
    src_index = LEDGER_REAL + ".index.json"
    if os.path.exists(src_index):
        shutil.copy2(src_index, str(workdir / "ledger.log.index.json"))
    src_last_hash = LEDGER_REAL + ".last_hash.json"
    if os.path.exists(src_last_hash):
        shutil.copy2(src_last_hash, str(workdir / "ledger.log.last_hash.json"))
    blobs_src = os.path.join(real_dir, "blobs")
    if os.path.isdir(blobs_src):
        os.symlink(blobs_src, str(workdir / "blobs"))

    copied_ledger = str(workdir / "ledger.log")

    # --- Ventana medida: SOLO la llamada bajo test ---
    t = time.time()
    result = distill_closure_decision(copied_ledger, closed_session_id)
    elapsed = time.time() - t

    # Rama endoso ejecutada sobre la copia (sin segunda decisión)
    assert result is not None
    assert result["payload"]["action"] == "distillation_endorsement"

    # Efecto real verificado en la copia: último evento appended = endoso
    with open(copied_ledger, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4096))
        tail = f.read().decode("utf-8")
    last_line = [l for l in tail.splitlines() if l.strip()][-1]
    assert json.loads(last_line)["event"]["event_type"] == EventType.CONTEXT_UPDATED.value

    assert elapsed < PERF_THRESHOLD_SECONDS, (
        f"{elapsed:.2f}s (umbral {PERF_THRESHOLD_SECONDS}s; "
        f"doble read_all() mide ~5.1s)"
    )


def test_distill_closure_endorsement_branch_preserved(temp_ledger):
    """Rama endoso (concurrencia): si ya existe la GOVERNANCE_DECISION para
    el summary, se appended UN CONTEXT_UPDATED de endoso y NO una segunda
    decisión.
    """
    ledger_path = temp_ledger
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    target_session_id = "sess-x"
    summary_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())

    # 1 SESSION_SUMMARY con session_id en payload
    writer.append(CanonicalEvent.from_dict({
        "event_id": summary_id,
        "event_type": EventType.SESSION_SUMMARY.value,
        "timestamp": datetime.now().isoformat(),
        "source": "harvester:opencode",
        "ctx_id": "endorse-ctx",
        "payload": {
            "session_id": target_session_id,
            "tool": "opencode",
            "turn_count": 3,
            "summary_lines": ["user: hola"],
            "errors": [],
        },
    }))
    # 1 GOVERNANCE_DECISION ya existente con parent = summary.event_id
    writer.append(CanonicalEvent.from_dict({
        "event_id": decision_id,
        "event_type": EventType.GOVERNANCE_DECISION.value,
        "timestamp": datetime.now().isoformat(),
        "source": "causadb:distill",
        "source_type": "agent",
        "ctx_id": "revive",
        "parent_event_id": summary_id,
        "payload": {
            "decision_type": "tactical",
            "impact": "low",
            "origin": "distill",
            "reasoning": "Cierre previo",
        },
    }))

    result = distill_closure_decision(ledger_path, target_session_id)

    # El retorno es el dict del endoso
    assert result is not None
    assert result["event_type"] == EventType.CONTEXT_UPDATED.value
    assert result["payload"]["action"] == "distillation_endorsement"
    assert result["payload"]["original_decision_id"] == decision_id
    assert result["payload"]["target_session_id"] == target_session_id

    # Eventos REALES escritos en el ledger
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())

    endorsements = [
        ev for ev in events
        if ev.event_type == EventType.CONTEXT_UPDATED
        and ev.payload.get("action") == "distillation_endorsement"
    ]
    assert len(endorsements) == 1, "Debe appended exactamente UN endoso"
    assert endorsements[0].payload.get("original_decision_id") == decision_id

    gov_decisions = [
        ev for ev in events
        if ev.event_type == EventType.GOVERNANCE_DECISION
    ]
    assert len(gov_decisions) == 1, "NO debe crearse una segunda GOVERNANCE_DECISION"


def test_distill_closure_finds_summary_without_duplicates(temp_ledger):
    """Guardia anti-duplicado: con 1 SESSION_SUMMARY matching, la llamada
    crea EXACTAMENTE 1 GOVERNANCE_DECISION nueva con parent_event_id ==
    summary.event_id (verificado leyendo el ledger escrito).
    """
    ledger_path = temp_ledger
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    target_session_id = "sess-y"
    summary_id = str(uuid.uuid4())
    writer.append(CanonicalEvent.from_dict({
        "event_id": summary_id,
        "event_type": EventType.SESSION_SUMMARY.value,
        "timestamp": datetime.now().isoformat(),
        "source": "harvester:opencode",
        "ctx_id": "dedup-ctx",
        "payload": {
            "session_id": target_session_id,
            "tool": "opencode",
            "turn_count": 2,
            "summary_lines": ["user: fix"],
            "errors": [],
        },
    }))

    result = distill_closure_decision(ledger_path, target_session_id)

    assert result is not None
    assert result["event_type"] == EventType.GOVERNANCE_DECISION.value
    assert result["parent_event_id"] == summary_id

    # Verificar contra el estado REAL del ledger
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())

    gov_decisions = [
        ev for ev in events
        if ev.event_type == EventType.GOVERNANCE_DECISION
    ]
    assert len(gov_decisions) == 1, (
        f"Debe existir EXACTAMENTE 1 GOVERNANCE_DECISION, hay {len(gov_decisions)}"
    )
    assert gov_decisions[0].parent_event_id == summary_id
    assert "Cierre de sesión automático" in gov_decisions[0].payload.get("reasoning", "")
