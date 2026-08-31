"""FIX.GOV-AUTO-4 — Tests anti-teatro del auto-distill de decisiones.

Artículo III (test-first): estos tests se escriben ANTES de la
implementación y deben fallar en Red (el harvest aún no deriva
GOVERNANCE_DECISION y revive no promueve desde ``description``).

Artículo IX (fixture real): los raws usan el shape real de la puntita
opencode (``_harvest_source_opencode._part_to_raw``) y del motor
universal (``_agent_transcript.agent_message_to_raw``): REASONING_STEP
lleva ``description`` (NO ``reasoning``) — field bug C1.

Cobertura:
  1. Harvest de REASONING_STEP(decision) con keyword → GOVERNANCE_DECISION
     origin='distill' con parent = event_id real (32-hex, UUID-válido).
  2. Idempotencia anti-teatro con cursor reseteado: 2ª corrida reescribe
     el REASONING_STEP (mismo event_id) pero NO duplica la decisión
     (dedup por parent).
  3. Reasoning sin keywords → score < 0.5 → NO promueve.
  4. COMMAND_RUN destructivo → GOVERNANCE_DECISION impact='critical'.
  5. REASONING_STEP con description LARGA (blob-ificada) → revive
     promueve leyendo desde el blob (corrección C4, resolve_blobs=True).
"""

import json
import uuid

import pytest
from types import MappingProxyType

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester
from causadb._init import causadb_init
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import _promote_decisions_to_governance


# ===================================================================
# Mock source — patrón de test_harvester_dedup.py (cursor secuencial)
# ===================================================================

class MockSource(HarvestSource):
    """Fuente mock con cursor secuencial ``{"index": N}``.

    ``source_type`` es configurable (default "mock", no-agente) para
    cubrir el requisito de FIX.GOV-AUTO-1 de derivar para TODAS las
    fuentes (no solo agent sources).
    """

    def __init__(self, ledger_path, events=None, source_type="mock"):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._type = source_type

    def source_type(self):
        return self._type

    def cursor_key(self):
        return f"{self._type}_cursor"

    def detect(self):
        return True

    def harvest(self, cursor=None):
        if not self._events:
            return []
        if cursor is None:
            return list(self._events)
        idx = cursor.get("index", 0)
        if idx >= len(self._events):
            return []
        return list(self._events[idx:])


# ===================================================================
# Helpers
# ===================================================================

def _decision_raw(description, step_type="decision", **extra):
    """Raw dict con el shape real de la puntita opencode: REASONING_STEP
    con ``description`` (NO ``reasoning``) — field bug C1."""
    raw = {
        "type": "REASONING_STEP",
        "timestamp": "2024-06-01T10:00:00Z",
        "step_type": step_type,
        "step_hash": "deadbeef",
        "subject": "security decision",
        "description": description,
        "agent": "opencode",
    }
    raw.update(extra)
    return raw


def _ledger_events(ledger):
    """Todos los eventos del ledger como dicts (resolve_blobs=True)."""
    reader = LedgerReader(ledger)
    return [entry["event"] for entry in reader.read_all_entries()]


# ===================================================================
# Tests
# ===================================================================

def test_harvest_generates_distill_decision(tmp_path):
    """Harvest de un REASONING_STEP(decision) con keyword → el ledger
    contiene un GOVERNANCE_DECISION origin='distill' cuyo parent_event_id
    es el event_id REAL del REASONING_STEP (32-hex, UUID-válido)."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    raw = _decision_raw("We must fix the critical security vulnerability now")
    source = MockSource(ledger, events=[raw])
    h = Harvester(ledger, config)
    h.register_source(source)

    result = h.harvest_all()
    assert result["mock"] == 1, f"expected 1 harvested event, got {result}"

    events = _ledger_events(ledger)
    reasoning_steps = [e for e in events if e["event_type"] == "REASONING_STEP"]
    gov = [e for e in events if e["event_type"] == "GOVERNANCE_DECISION"]

    assert len(reasoning_steps) == 1
    assert len(gov) == 1, (
        f"expected 1 GOVERNANCE_DECISION from harvest, got {len(gov)}"
    )

    g = gov[0]
    assert g["payload"]["origin"] == "distill"
    assert g["payload"]["impact"] in {"critical", "high", "medium"}
    assert g["ctx_id"] == "harvester:mock"
    # parent = event_id real del REASONING_STEP (no step_hash 64-hex)
    assert g["parent_event_id"] == reasoning_steps[0]["event_id"]
    uuid.UUID(g["parent_event_id"])  # debe ser UUID-válido (32-hex)


def test_harvest_decision_idempotent_with_cursor_reset(tmp_path):
    """Anti-teatro (Art. IX): 2ª corrida con cursor reseteado reescribe
    el REASONING_STEP (mismos event_ids — dedup real, no cursor) pero NO
    genera un segundo GOVERNANCE_DECISION (dedup por parent_event_id)."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    raw = _decision_raw("Migrating the database is a breaking change for all clients")
    source = MockSource(ledger, events=[raw])
    h = Harvester(ledger, config)
    h.register_source(source)

    # --- 1ª corrida: 1 evento + 1 decisión ---
    r1 = h.harvest_all()
    assert r1["mock"] == 1
    gov1 = [e for e in _ledger_events(ledger) if e["event_type"] == "GOVERNANCE_DECISION"]
    assert len(gov1) == 1, f"1ª corrida: esperaba 1 decisión, got {len(gov1)}"

    # --- MUTACIÓN: ignorar el cursor (la fuente re-cosecha el mismo raw) ---
    original_load = h._load_cursors
    h._load_cursors = lambda: {}

    # --- 2ª corrida: REASONING_STEP duplicado (mismo event_id) ---
    r2 = h.harvest_all()
    assert r2["mock"] == 1, (
        f"2ª corrida con cursor ignorado: esperaba 1 duplicado, got {r2}"
    )

    events2 = _ledger_events(ledger)
    reasoning2 = [e for e in events2 if e["event_type"] == "REASONING_STEP"]
    gov2 = [e for e in events2 if e["event_type"] == "GOVERNANCE_DECISION"]

    assert len(reasoning2) == 2, (
        f"esperaba 2 REASONING_STEP (duplicado real), got {len(reasoning2)}"
    )
    assert reasoning2[0]["event_id"] == reasoning2[1]["event_id"], (
        "mismo raw → mismo SHA-256 → mismo event_id (duplicado real)"
    )
    assert len(gov2) == 1, (
        f"dedup por parent: esperaba exactamente 1 GOVERNANCE_DECISION, "
        f"got {len(gov2)}"
    )
    h._load_cursors = original_load


def test_no_keywords_no_decision(tmp_path):
    """REASONING_STEP(decision) sin keywords (score < 0.5) → NO genera
    GOVERNANCE_DECISION."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    raw = _decision_raw("Considering the layout of the new module and its helpers")
    source = MockSource(ledger, events=[raw])
    h = Harvester(ledger, config)
    h.register_source(source)

    h.harvest_all()

    gov = [e for e in _ledger_events(ledger) if e["event_type"] == "GOVERNANCE_DECISION"]
    assert len(gov) == 0, f"sin keywords → no decision, got {len(gov)}"


def test_destructive_command_generates_critical(tmp_path):
    """COMMAND_RUN destructivo → GOVERNANCE_DECISION impact='critical'
    origin='distill' decision_type='tactical'."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    raw = {
        "type": "COMMAND_RUN",
        "timestamp": "2024-06-01T10:00:00Z",
        "command": "rm -rf /",
    }
    source = MockSource(ledger, events=[raw])
    h = Harvester(ledger, config)
    h.register_source(source)

    h.harvest_all()

    gov = [e for e in _ledger_events(ledger) if e["event_type"] == "GOVERNANCE_DECISION"]
    assert len(gov) == 1, f"esperaba 1 decisión por comando destructivo, got {len(gov)}"
    assert gov[0]["payload"]["impact"] == "critical"
    assert gov[0]["payload"]["origin"] == "distill"
    assert gov[0]["payload"]["decision_type"] == "tactical"


def test_decision_blobified_promoted(tmp_path):
    """REASONING_STEP(decision) con description LARGA → payload blob-ificado
    al escribir. Revive (Capa 0) debe promover leyendo con resolve_blobs=True
    (corrección C4) — 28% de las decisiones reales están blob-ificadas."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    long_desc = "Security compliance requires a critical migration of all " + "x" * 2000
    writer.append(CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="harvester:opencode",
        source="harvester:opencode",
        payload=MappingProxyType({
            "step_type": "decision",
            "step_hash": "abc123",
            "subject": "security migration",
            "description": long_desc,
            "agent": "opencode",
        }),
    ))

    # Verificar que el payload del REASONING_STEP se blob-ificó en el
    # ledger crudo (payload serializado > blob_store_threshold=1024).
    with open(ledger) as f:
        raw_lines = [json.loads(l) for l in f if l.strip()]
    rs_raw = next(l for l in raw_lines if l["event"]["event_type"] == "REASONING_STEP")
    assert "$blob" in rs_raw["event"]["payload"], (
        "REASONING_STEP payload debe estar blob-ificado (description larga)"
    )

    # Revive promueve desde la description resuelta del blob.
    written = _promote_decisions_to_governance(ledger)
    assert written == 1, f"esperaba 1 promoción desde blob, got {written}"

    gov = [e for e in _ledger_events(ledger) if e["event_type"] == "GOVERNANCE_DECISION"]
    assert len(gov) == 1
    assert gov[0]["payload"]["reasoning"] == long_desc
    rs = [e for e in _ledger_events(ledger) if e["event_type"] == "REASONING_STEP"]
    assert gov[0]["parent_event_id"] == rs[0]["event_id"]
