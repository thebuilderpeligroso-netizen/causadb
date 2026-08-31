"""FIX.GOV-AUTO — Auto-distill de decisiones (Capa 0 unificada).

Deriva ``GOVERNANCE_DECISION`` events automáticamente desde el harvest y
el revive, sin cooperación del agente: la captura de DECISIÓN no depende
de que el agente se acuerde de llamar ``causadb_log_decision``.

Dos callers reales (Artículo VIII — sin abstracciones con 0
implementaciones):
  - ``causadb._harvester`` (FIX.GOV-AUTO-1): deriva en ``_flush_batch``
    sobre ``events[:written]``, cubriendo TODAS las fuentes (agente +
    shell/n8n) sin depender del ``raw_events_buffer`` ni del cap de 5000.
  - ``causadb.cli._cmd_revive`` (FIX.GOV-AUTO-2): la Capa 0 de revive
    delega acá, reparando el field bug (``reasoning``→``description``),
    el cap de 1000 de ``LedgerIndex.query`` y el parent no-UUID.

Repara el encadenamiento causal (FIX.GOV-AUTO-3): ``parent_event_id`` es
el ``event_id`` REAL del evento fuente (32-hex, UUID-válido), no el
``step_hash`` — permite ``causadb_trace``/``why`` desde la decisión hasta
el razonamiento.

Defensa en profundidad: cada candidato se valida con
``validate_event_schema`` antes de devolverse (``LedgerWriter.append`` NO
valida schema — el defecto quedaba latente). El razonamiento se redacta
antes de construir el payload.
"""

import logging
import os
import uuid
from typing import Optional, Dict, Any
from types import MappingProxyType

from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter
from causadb._redactor import redact_payload
from causadb._schema_validator import validate_event_schema

# Umbral de impacto para promover (C2 — fórmula exacta).
MIN_IMPACT_SCORE = 0.5

# Keywords weights for impact scoring (C2 — fórmula exacta).
KEYWORD_WEIGHTS = {
    "critical": 0.6, "breaking": 0.5, "migration": 0.4, "revert": 0.4,
    "incompatible": 0.5, "must": 0.3, "urgent": 0.4,
    "security": 0.6, "compliance": 0.5, "data-loss": 0.7,
}

# Destructive command patterns (Capa 0b).
DESTRUCTIVE_PATTERNS = [
    "rm -rf /", "rm -rf / ", "git push --force", "DROP TABLE",
    "ALTER TABLE", "chmod 777", "rm -rf .git", ":(){ :|:& };:",
    "rm -rf /home", "rm -fr ", "mkfs.", "dd if=",
]


def _compute_impact_score(reasoning: str, confidence: Optional[float] = None) -> float:
    """Calculate impact score for a REASONING_STEP reasoning text.

    Uses KEYWORD_WEIGHTS dictionary with confidence bonus.

    Args:
        reasoning: The reasoning text to analyze.
        confidence: Optional confidence float [0, 1].

    Returns:
        Score in [0.0, 1.0].
    """
    reasoning_lower = reasoning.lower()
    score = max(
        (KEYWORD_WEIGHTS.get(kw, 0.0) for kw in KEYWORD_WEIGHTS if kw in reasoning_lower),
        default=0.0,
    )
    if confidence is not None and confidence > 0.7:
        score = min(1.0, score + 0.2)
    return score


def _is_destructive_command(command: str) -> bool:
    """Check if a command matches destructive patterns."""
    cmd_lower = command.lower()
    for pattern in DESTRUCTIVE_PATTERNS:
        if pattern.lower() in cmd_lower:
            return True
    return False


def _safe_reasoning(reasoning: str, config: CausaDBConfig) -> str:
    """Redacta el razonamiento antes de escribirlo (defensa en profundidad)."""
    redacted = redact_payload({"reasoning": reasoning}, config)
    return redacted.get("reasoning", reasoning)


def _build_decision_event(
    ev: CanonicalEvent,
    config: CausaDBConfig,
    ctx_id: str,
) -> Optional[CanonicalEvent]:
    """Construye un GOVERNANCE_DECISION desde un REASONING_STEP(decision).

    Lee el razonamiento de ``description`` con fallback a ``reasoning``
    (field bug C1: los raws cosechados por opencode y el motor universal
    llevan ``description``). Promueve solo si ``_compute_impact_score >=
    0.5``. parent = ``ev.event_id`` real.

    Returns:
        Candidato o None si no aplica (step_type != decision, sin texto,
        o score < umbral).
    """
    payload = dict(ev.payload)
    if payload.get("step_type") != "decision":
        return None
    reasoning = payload.get("description") or payload.get("reasoning") or ""
    if not reasoning:
        return None

    score = _compute_impact_score(reasoning, payload.get("confidence"))
    if score < MIN_IMPACT_SCORE:
        return None

    if score >= 0.8:
        impact = "critical"
    elif score >= 0.6:
        impact = "high"
    else:
        impact = "medium"

    return CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id=ctx_id,
        source="causadb:distill",
        source_type="agent",
        parent_event_id=ev.event_id,
        payload=MappingProxyType({
            "reasoning": _safe_reasoning(reasoning, config),
            "impact": impact,
            "decision_type": "architectural" if score >= 0.7 else "strategic",
            "origin": "distill",
        }),
    )


def _build_destructive_event(
    ev: CanonicalEvent,
    config: CausaDBConfig,
    ctx_id: str,
) -> Optional[CanonicalEvent]:
    """Construye un GOVERNANCE_DECISION desde un COMMAND_RUN destructivo.

    parent = ``ev.event_id`` real. Returns None si el comando no matchea
    DESTRUCTIVE_PATTERNS.
    """
    payload = dict(ev.payload)
    command = payload.get("command", "")
    if not _is_destructive_command(command):
        return None

    return CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id=ctx_id,
        source="causadb:distill",
        source_type="agent",
        parent_event_id=ev.event_id,
        payload=MappingProxyType({
            "reasoning": _safe_reasoning(f"Destructive command detected: {command}", config),
            "impact": "critical",
            "decision_type": "tactical",
            "origin": "distill",
        }),
    )


def candidate_decision_events(
    events: list[CanonicalEvent],
    config: CausaDBConfig,
    ctx_id: str,
) -> list[CanonicalEvent]:
    """Deriva candidatos GOVERNANCE_DECISION desde eventos fuente.

    Por cada evento de entrada:
      - ``REASONING_STEP`` con ``step_type == "decision"`` y score de
        impacto >= 0.5 → GOVERNANCE_DECISION origin='distill'.
      - ``COMMAND_RUN`` destructivo → GOVERNANCE_DECISION impact='critical'
        origin='distill'.

    Cada candidato se valida con ``validate_event_schema`` antes de
    devolverse; los inválidos se descartan (con warning) — defensa en
    profundidad sobre ``LedgerWriter.append`` que NO valida.

    Args:
        events: eventos fuente (CanonicalEvent ya construidos, con
            ``event_id`` real disponible).
        config: config para redacción.
        ctx_id: ctx_id para las decisiones derivadas (ej.
            ``harvester:opencode``, ``revive``).

    Returns:
        Candidatos GOVERNANCE_DECISION válidos SIN dedup — el caller
        filtra por parent contra ``load_existing_decision_parents``.
    """
    candidates: list[CanonicalEvent] = []
    for ev in events:
        if ev.event_type == EventType.REASONING_STEP:
            gov = _build_decision_event(ev, config, ctx_id)
        elif ev.event_type == EventType.COMMAND_RUN:
            gov = _build_destructive_event(ev, config, ctx_id)
        else:
            continue
        if gov is None:
            # REASONING_STEP no-decision (step_type != "decision") o
            # COMMAND_RUN no-destructivo → no aplica.
            continue

        result = validate_event_schema(gov)
        if not result.is_valid:
            logging.warning(
                "Decision distill: candidate invalid (%s: %s), dropped",
                result.failure_type, result.description,
            )
            continue
        candidates.append(gov)
    return candidates


def _build_closure_decision(summary_event: Dict[str, Any], origin: str = "distill") -> Dict[str, Any]:
    """Crea una GOVERNANCE_DECISION de cierre destilada pasivamente."""
    payload = summary_event.get("payload") or {}
    tool = payload.get("tool") or "unknown"
    turn_count = payload.get("turn_count") or 0
    errors = payload.get("errors") or []
    summary_lines = payload.get("summary_lines") or []
    summary_text = ", ".join(summary_lines)[:150] if summary_lines else "Sin resumen disponible"
    
    reasoning = (
        f"Cierre de sesión automático (distilado pasivo). "
        f"Tool: {tool}. Turnos: {turn_count}. Errores: {len(errors)}. "
        f"Resumen: {summary_text}"
    )
    
    return {
        "event_type": EventType.GOVERNANCE_DECISION.value,
        "parent_event_id": summary_event.get("event_id"),
        "payload": {
            "decision_type": "tactical",
            "impact": "low",
            "origin": origin,
            "reasoning": reasoning,
            "bit_id": "BIT-CHR.119"
        }
    }


def distill_closure_decision(ledger_path: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Destila y escribe una decisión de cierre si no existe ya para la sesión.

    Fase 3 (perf): usa LedgerIndex.query (índice real) en vez de read_all().
    - Pasada 1: solo SESSION_SUMMARY (169 hoy) con include_payloads=True
      (OBLIGATORIO: session_id vive en payload y _slim_payload lo elimina;
      además 13/169 están $blob-ificados).
    - Pasada 2: filtro indexado parent_event_id (sin traer las 230 decisiones).
    Nota: limit=None aplica cap duro MAX_QUERY_LIMIT=1000 (LedgerIndex) —
    suficiente para summaries/decisiones actuales; documentado, no silencioso.
    """
    from causadb._ledger_index import LedgerIndex

    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)

    # Pasada 1 — buscar summary de esta sesión (dicts crudos, SIN CanonicalEvent:
    # from_dict exige claves exactas y rompe con metadata desconocida — bug BIT-CHR.35 P1)
    index = LedgerIndex(ledger_path)
    summary_entries = index.query(
        event_type=EventType.SESSION_SUMMARY.value,
        limit=None,
        include_payloads=True,
    )
    summary_event = None
    for entry in summary_entries:
        event = entry["event"]
        payload = event.get("payload") or {}
        val = payload.get("session_id") or payload.get("__harvest_session_id")
        if str(val) == str(session_id):
            summary_event = event  # último match gana (append order)

    if summary_event is None:
        return None

    # Pasada 2 — filtro indexado directo por parent_event_id (no traer todas las decisiones)
    existing_entries = index.query(
        event_type=EventType.GOVERNANCE_DECISION.value,
        parent_event_id=summary_event["event_id"],
        limit=None,
        include_payloads=False,
    )

    if existing_entries:
        # Ya existe: registrar endoso (concurrencia)
        actor = os.getenv("CAUSADB_AGENT") or os.getenv("USER") or "causadb:subagent"
        endorsement_dict = {
            "event_id": str(uuid.uuid4()),
            "event_type": EventType.CONTEXT_UPDATED.value,
            "source": "causadb:distill",
            "source_type": "agent",
            "ctx_id": "revive",
            "payload": {
                "action": "distillation_endorsement",
                "target_session_id": session_id,
                "original_decision_id": existing_entries[0]["event"]["event_id"],
                "endorsed_by": actor,
            },
        }
        endorsement_event = CanonicalEvent.from_dict(endorsement_dict)
        writer.append(endorsement_event)
        return endorsement_dict

    # Escribir nueva decisión
    decision_dict = {
        "event_id": str(uuid.uuid4()),
        "event_type": EventType.GOVERNANCE_DECISION.value,
        "parent_event_id": summary_event["event_id"],
        "source": "causadb:distill",
        "source_type": "agent",
        "ctx_id": "revive",
        "payload": {
            "decision_type": "tactical",
            "impact": "low",
            "origin": "distill",
            "reasoning": _build_closure_decision(summary_event)["payload"]["reasoning"],
            "bit_id": "BIT-CHR.119",
        },
    }

    # Validar schema antes de escribir
    decision_event = CanonicalEvent.from_dict(decision_dict)
    result = validate_event_schema(decision_event)
    if not result.is_valid:
        logging.warning("Decision distill: candidate invalid (%s), dropped", result.description)
        return None

    writer.append(decision_event)
    return decision_dict


def load_existing_decision_parents(ledger_path: str) -> set[str]:
    """Parents de GOVERNANCE_DECISION ya existentes (para dedup).

    Una pasada via ``LedgerReader.read_all_entries`` filtrando
    GOVERNANCE_DECISION y recolectando ``parent_event_id`` no-None.

    ``resolve_blobs=False`` es suficiente y deliberado:
    ``parent_event_id`` es un campo top-level del evento, nunca se
    blob-ifica (el BlobStore solo externaliza payloads) — evita la
    resolución de blobs sobre ledgers grandes y el fail-fast de
    ``resolve_payload`` ante un blob faltante. La corrección C4
    (resolve_blobs=True) aplica a la LECTURA de contenido de decisiones
    (payloads blob-ificados), no a este set de parents.

    Args:
        ledger_path: path absoluto del ledger.

    Returns:
        Set de ``parent_event_id`` (str) de las decisiones existentes.
    """
    parents: set[str] = set()
    reader = LedgerReader(ledger_path)
    for entry in reader.read_all_entries(resolve_blobs=False):
        event = entry.get("event", {})
        if event.get("event_type") != "GOVERNANCE_DECISION":
            continue
        parent = event.get("parent_event_id")
        if parent:
            parents.add(parent)
    return parents
