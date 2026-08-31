"""R.2 — `causadb revive` command.

Generates volatile revival context for agent bootstrap after a crash or
session break. Combines:

  - Capa 0 (Framework): Distill heurístico promueve REASONING_STEP(decision)
    de alto impacto + COMMAND_RUN(destructive) a GOVERNANCE_DECISION events.
  - Capa 1 (Agent): GOVERNANCE_DECISION events logueados explícitamente
    via causadb_log_decision MCP tool.
  - Resume state: from OCB + ReplayEngine (reuses _cmd_resume).

Pipeline:
  1. Workspace discovery.
  2. _promote_decisions_to_governance() — Capa 0 heuristic promotion.
  3. generate_resume() — technical state summary.
  4. Query governance decisions via replay.
  5. Combine into structured output (json or markdown).

Artículo II: Thin wrapper — no duplicated nucleus logic.
Artículo V: No carga el ledger raw al modelo. Solo eventos estructurados.
Artículo VIII: No crea abstracciones con 0 implementaciones.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from causadb._decision_distill import (
    KEYWORD_WEIGHTS,
    DESTRUCTIVE_PATTERNS,
    _compute_impact_score,
    _is_destructive_command,
    candidate_decision_events,
    load_existing_decision_parents,
)
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb._config import CausaDBConfig


FILE_TREE_MAX_LINES = 200

# C2 — Cap de bytes en revive (Gap de producto). ``MAX_REVIVE_BYTES`` es el
# tope por defecto para el output de revive. Configurable vía env
# ``CAUSADB_MAX_REVIVE_BYTES`` — se lee en call-time (patrón os.getenv
# directo) para permitir ajuste por deployment y monkeypatch en tests.
MAX_REVIVE_BYTES = 300_000


def _max_revive_bytes() -> int:
    """Resolver el cap de bytes efectivo de revive (env > default, mínimo 1).

    El mínimo 1 es un guard anti loop infinito: el trim de decisiones
    recorta de a una y el loop termina cuando ``len(decisions) == 1``.
    """
    raw = os.getenv("CAUSADB_MAX_REVIVE_BYTES")
    if raw is None:
        return MAX_REVIVE_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        return MAX_REVIVE_BYTES


def _render_file_tree_skill(content: str, max_lines: int = FILE_TREE_MAX_LINES) -> str:
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content
    
    truncated = lines[:max_lines]
    return "\n".join(truncated) + f"\n[...] +{len(lines) - max_lines} líneas más — usá causadb_skill_list (skill_type=file_tree) para ver el mapa completo"


# ============================================================================
# Capa 0 — Heuristic promotion (Distill heurístico)
#
# FIX.GOV-AUTO-2 — La lógica de Capa 0 vive en ``_decision_distill``
# (KEYWORD_WEIGHTS, DESTRUCTIVE_PATTERNS, _compute_impact_score,
# _is_destructive_command, candidate_decision_events). Este módulo la
# re-exporta (compat) y delega en ella: motor único compartido con el
# harvest (Artículo II — thin wrapper, sin duplicar lógica).
# ============================================================================

def _detect_destructive_commands(ledger_path: str) -> int:
    """Capa 0b — Escanear COMMAND_RUN events por comandos peligrosos.

    Delega en ``_decision_distill.candidate_decision_events``: para cada
    COMMAND_RUN cuyo comando coincida con DESTRUCTIVE_PATTERNS, escribe
    un GOVERNANCE_DECISION event con origin='distill', impact='critical'.
    Deduplicado por parent_event_id contra las decisiones existentes,
    iterando via LedgerIndex query (O(1) index lookup) con fallback a
    LedgerReader streaming (resolve_blobs=True) — sin el cap
    de 1000 de ``LedgerIndex.query``.

    Returns:
        Número de governance decisions escritas.
    """
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)
    existing_parents = load_existing_decision_parents(ledger_path)

    # Try index query first (O(1) lookup), fallback to full scan
    command_events = []
    try:
        from causadb._ledger_index import LedgerIndex
        index = LedgerIndex(ledger_path)
        # Query for COMMAND_RUN events - no limit to get all
        entries = index.query(
            event_type="COMMAND_RUN",
            limit=None,
            include_payloads=True,
        )
        for entry in entries:
            event = entry.get("event")
            if event and _is_destructive_command(dict(event.get("payload", {})).get("command", "")):
                command_events.append(CanonicalEvent.from_dict(event))
    except Exception:
        # Fallback: full scan via LedgerReader (streaming, resolve_blobs=True)
        reader = LedgerReader(ledger_path)
        command_events = [
            ev for ev in reader.read_all()
            if ev.event_type == EventType.COMMAND_RUN
            and _is_destructive_command(dict(ev.payload).get("command", ""))
        ]
    candidates = candidate_decision_events(command_events, config, ctx_id="revive")
    candidates = [c for c in candidates if c.parent_event_id not in existing_parents]

    written = 0
    for gov in candidates:
        writer.append(gov)
        if gov.parent_event_id:
            existing_parents.add(gov.parent_event_id)
        written += 1
    return written


def _promote_decisions_to_governance(ledger_path: str) -> int:
    """Capa 0a — Promover REASONING_STEP(decision) de alto impacto a governance.

    Delega en ``_decision_distill.candidate_decision_events`` (motor
    único compartido con el harvest). Corrige el field bug (lee
    ``description`` con fallback a ``reasoning`` — los raws cosechados
    llevan ``description``), itera TODAS las decisiones via LedgerIndex
    query (O(1) index lookup) con fallback a LedgerReader streaming
    (sin cap) y usa el ``event_id`` real del REASONING_STEP
    como ``parent_event_id`` (encadenamiento causal, FIX.GOV-AUTO-3).

    Returns:
        Número de governance decisions escritas.
    """
    config = CausaDBConfig(ledger_path=ledger_path)
    writer = LedgerWriter(ledger_path, config)
    existing_parents = load_existing_decision_parents(ledger_path)

    # Try index query first (O(1) lookup), fallback to full scan
    reasoning_events = []
    try:
        from causadb._ledger_index import LedgerIndex
        index = LedgerIndex(ledger_path)
        # Query for REASONING_STEP events - no limit to get all
        entries = index.query(
            event_type="REASONING_STEP",
            limit=None,
            include_payloads=True,
        )
        for entry in entries:
            event = entry.get("event")
            if event and dict(event.get("payload", {})).get("step_type") == "decision":
                reasoning_events.append(CanonicalEvent.from_dict(event))
    except Exception:
        # Fallback: full scan via LedgerReader (streaming, no cap)
        import logging
        logging.warning(
            "LedgerIndex query failed, falling back to LedgerReader.read_all() for promotion"
        )
        reader = LedgerReader(ledger_path)
        reasoning_events = [
            ev for ev in reader.read_all()
            if ev.event_type == EventType.REASONING_STEP
            and dict(ev.payload).get("step_type") == "decision"
        ]

    candidates = candidate_decision_events(reasoning_events, config, ctx_id="revive")
    candidates = [c for c in candidates if c.parent_event_id not in existing_parents]

    written = 0
    for gov in candidates:
        writer.append(gov)
        if gov.parent_event_id:
            existing_parents.add(gov.parent_event_id)
        written += 1
    return written


# ============================================================================
# Capa 1 — Revive output generators
# ============================================================================

def _generate_governance_decisions(
    ledger_path: str,
    max_decisions: int = 10,
    state: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Query GOVERNANCE_DECISION events via replay engine.

    Si ``state`` es provisto (pre-computado por el caller), lo reusa y NO
    construye un ``ReplayEngine`` nuevo. Si es ``None``, construye uno
    (path legacy usado por CLI directo u otros callers).
    """
    try:
        if state is None:
            engine = ReplayEngine(ledger_path)
            state = engine.reconstruct_state()
    except Exception:
        return []

    decisions = state.get("governance_decisions", [])
    # Newest first (reverse chronological)
    decisions = list(reversed(decisions))
    return decisions[:max_decisions]


def _generate_agent_recent_activity(
    ledger_path: str,
    max_decisions: int = 5,
    max_files: int = 5,
) -> List[Dict[str, Any]]:
    """Q.3 — Intención reciente del agente (Capa 1) sin replay completo.

    Consulta directa y barata (Art. V): GOVERNANCE_DECISION origin=agent +
    FILE_MODIFIED, vía ``query_events`` (no ``ReplayEngine.reconstruct_state``).
    Esto complementa la REGLA 1: al cerrar sesión se loguea un
    GOVERNANCE_DECISION documentando qué se hizo y qué sigue — esta sección
    lo hace visible al abrir la próxima sesión sin excavar blobs.

    Args:
        ledger_path: Absolute path to the ledger.
        max_decisions: Max agent-origin decisions to include (newest first).
        max_files: Max recently modified files to include (newest first).

    Returns:
        List of dicts: ``{"kind": "decision"|"file", ...}``, newest first.
        Empty list on error (degrade, no crash — same pattern as
        ``_generate_governance_decisions``, Art. VIII fail-closed).
    """
    try:
        from causadb._query_engine import query_events

        # limit=50 aplica el cap ANTES de resolver blobs (Art. V): no resolvemos
        # payloads de TODOS los GOVERNANCE_DECISION/FILE_MODIFIED para truncar a
        # 5 después (MENOR-Checker Q.3).
        decisions = query_events(
            ledger_path,
            event_type=EventType.GOVERNANCE_DECISION.value,
            intent_only=False,
            resolve_blobs=True,
            limit=50,
        )
        agent_decisions = [
            d for d in decisions if (d.get("payload") or {}).get("origin") == "agent"
        ]
        # query_events returns ascending (append order); newest first.
        agent_decisions = list(reversed(agent_decisions))[:max_decisions]

        files = query_events(
            ledger_path,
            event_type=EventType.FILE_MODIFIED.value,
            intent_only=False,
            resolve_blobs=True,
            limit=50,
        )
        files = list(reversed(files))[:max_files]

        activity: List[Dict[str, Any]] = []
        for d in agent_decisions:
            payload = d.get("payload") or {}
            activity.append({
                "kind": "decision",
                "timestamp": d.get("timestamp"),
                "event_id": d.get("event_id"),
                "reasoning": (payload.get("reasoning") or "")[:200],
                "decision_type": payload.get("decision_type"),
                "impact": payload.get("impact"),
            })
        for f in files:
            payload = f.get("payload") or {}
            activity.append({
                "kind": "file",
                "timestamp": f.get("timestamp"),
                "event_id": f.get("event_id"),
                "path": payload.get("path"),
            })
        return activity
    except Exception:
        return []


def _generate_tool_instructions() -> List[Dict[str, Any]]:
    """Generate tool instructions for the revive context.

    This gives the agent knowledge of the causadb_log_decision tool
    without needing tool_patterns (which requires count > 1).
    """
    return [
        {
            "name": "causadb_log_decision",
            "description": (
                "Log a governance decision to the CausaDB ledger. "
                "Use this when you make a strategic, architectural, "
                "tactical, or revert decision that affects project direction."
            ),
            "usage": (
                "causadb_log_decision(reasoning, impact, decision_type, "
                "origin='agent', ...)"
            ),
            "parameters": {
                "reasoning": "str (required) — the decision reasoning",
                "impact": "str (required) — critical|high|medium|low",
                "decision_type": "str (required) — strategic|architectural|tactical|revert",
                "origin": "str (optional, default='agent') — agent|distill",
            },
        },
    ]


def _generate_drill_down_instructions(
    resume: Dict[str, Any],
    observations: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """BIT-14.8 — Generate conditional drill-down instructions.

    Only shows tools that are relevant to the ledger content.
    A static list of 5 tools when the ledger is empty would be teatro
    (Artículo IX): the agent sees options it cannot exercise.

    R.1.5 — Cuando hay observaciones pendientes (resolved_reason is None),
    agrega una línea sugerida `causadb_why(file_path, line_number)` con los
    paths CONCRETOS del primer entry pendiente (no strings genéricos).

    Args:
        resume: The resume dict from generate_resume().
        observations: Optional list of observation entries from
            state["observations"] (replay). Each entry has keys:
            file_path, line_number, description, severity, resolved_reason.

    Returns:
        List of markdown lines for the "Para profundizar" section.
        Empty list if nothing to drill down on (except causadb_impact).
    """
    lines = []

    if resume.get("unique_files_count", 0) > 0 or resume.get("files_modified", 0) > 0:
        lines.append(
            '- `causadb_query(event_type="FILE_MODIFIED")` '
            "— detalle de cada evento de modificación"
        )
        lines.append(
            "- `causadb_why(file_path, line_number)` "
            "— atribuye una línea al evento que la introdujo"
        )
        lines.append(
            "- `causadb_trace(file_path, line_number)` "
            "— traza el cono causal upstream de una línea"
        )

    if resume.get("unique_commands_count", 0) > 0 or resume.get("commands_run_count", 0) > 0:
        lines.append(
            '- `causadb_query(event_type="COMMAND_RUN")` '
            "— detalle de comandos ejecutados"
        )

    # R.1.5 — Sugerencia concreta de causadb_why cuando hay observaciones
    # pendientes. Usa los paths reales del primer entry pendiente (no strings
    # genéricos) — anti-teatro artículo IX. BIT-CHR.34: observaciones sin
    # file_path (ej. harvester:browser con file_path="unknown") no tienen
    # línea atribuible; se saltean al buscar el candidato.
    if observations:
        pending = [o for o in observations if o.get("resolved_reason") is None]
        first = next(
            (o for o in pending if o.get("file_path") not in (None, "unknown")),
            None,
        )
        if first is not None:
            fp = first.get("file_path", "unknown")
            ln = first.get("line_number", "N/A")
            lines.append(
                f"- `causadb_why({fp!r}, {ln})` "
                "— atribuye la línea al evento OBSERVATION que la introdujo"
            )

    # causadb_impact always shown — doesn't depend on specific event types
    lines.append(
        "- `causadb_impact(event_id)` — blast radius de un evento"
    )

    # F1 (M2) — Gap 4: puntero a las tools OCB
    lines.append(
        "- `causadb_ocb_status` — memoria granular de corto plazo "
        "(OCB = Operational Context Buffer, caché volátil L1 Art. V)"
    )
    if resume.get("preloaded_partitions"):
        lines.append(
            "- `causadb_ocb_load_partition(partition_id)` — detalle de una "
            "partición específica (resuelve refs $blob contra el BlobStore)"
        )

    return lines


def _generate_revive_markdown(data: Dict[str, Any]) -> str:
    """Render revive data as markdown."""
    lines = []
    
    # R4 — Banner de degradación por blobs faltantes (al tope, antes de todo)
    if data.get("degraded"):
        degraded_detail = data.get("degraded_detail", {})
        error_count = degraded_detail.get("error_count", 0)
        errors = degraded_detail.get("errors", [])
        lines.append("⚠️ **BLOBS FALTANTES** — El contexto está degradado\n")
        lines.append(f"Se detectaron **{error_count}** error(es) de blobs faltantes durante la reconstrucción.\n")
        if errors:
            lines.append("> Primer error:\n")
            first_err = errors[0]
            # Handle both string and dict formats for backward compat
            if isinstance(first_err, dict):
                lines.append(f"> Step: `{first_err.get('step', '?')}` — {first_err.get('error', '?')}\n")
            else:
                lines.append(f"> {first_err}\n")
            if len(errors) > 1:
                lines.append(f"> ... y {len(errors) - 1} error(es) más (ver JSON output para detalle completo)\n")
        lines.append("---\n")

    # R.4.2 — Alerta de deuda de trazabilidad (total collapse).
    # Se inyecta cuando la última sesión cerró abruptamente (abrupt_close)
    # y NO hay entry_summary (ningún SESSION_SUMMARY en el ledger).
    # Suprimida si ya existe una GOVERNANCE_DECISION que saldó la deuda
    # (origin=agent o distill con decision_type=tactical/revert).
    resume = data.get("resume", {})
    session_type = resume.get("session_type", "first_run")
    entry_summary = resume.get("entry_summary")
    decisions = data.get("governance_decisions", [])
    
    # Check if debt is already reconciled by a GOVERNANCE_DECISION
    debt_reconciled = any(
        d.get("origin") in ("agent", "distill") and d.get("decision_type") in ("tactical", "revert")
        for d in decisions
    )
    
    if session_type == "abrupt_close" and entry_summary is None and not debt_reconciled:
        lines.append("⚠️ **ALERTA DE TRAZABILIDAD:** La sesión anterior cerró abruptamente "
                     "y no hay resumen de entrada (deuda de trazabilidad (gobernanza pendiente)).\n")
        lines.append("> Ejecutá `causadb_log_decision` con `decision_type=tactical` o `revert` "
                     "para saldar la deuda y documentar el cierre.\n")
        lines.append("---\n")

    lines.append("# ⚡ Contexto de Revitalización (Volátil)\n")
    lines.append(f"Generated: {datetime.now().isoformat()}\n")
    lines.append(f"**Ledger:** `{data.get('ledger_path', 'N/A')}`\n")
    lines.append("> **Doctrina:** patrones de reconstrucción y auditoría en docs/canon.md — "
                 "leela con `causadb canon` (CLI) o el resource MCP `causadb://canon`.\n")

    # Project snapshots (new)
    snapshots = data.get("project_snapshots", [])
    if snapshots:
        last = snapshots[-1]
        lines.append("## Project Snapshot\n")
        lines.append(f"- **Eventos totales:** {last.get('total_events', 'N/A')}")
        lines.append(f"- **Tests:** {last.get('total_tests', 'N/A')}")
        lines.append(f"- **Fases completadas:** {', '.join(last.get('fases_completadas', []))}")
        lines.append(f"- **Bloqueantes resueltos:** {last.get('bloqueantes_resueltos', 'N/A')}")
        lines.append(f"- **Notas:** \"{last.get('notas', '')}\"\n")

    # Session summaries (Fase 11.4b)
    session_summaries = data.get("session_summaries", [])
    if session_summaries:
        lines.append("## Sesiones Recientes\n")
        for ss in session_summaries[-10:]:
            tool = ss.get("tool", "unknown")
            sid = ss.get("session_id", "unknown")
            tc = ss.get("turn_count", 0)
            tokens = ss.get("tokens_used", 0)
            dur = ss.get("duration_s", 0)
            lines.append(f"- **[{tool}]** `{sid}` — {tc} turnos, {tokens} tokens, {dur}s")
            summary_lines = ss.get("summary_lines", [])
            for sl in summary_lines[:3]:
                lines.append(f"  - {sl}")
            errors = ss.get("errors", [])
            if errors:
                lines.append(f"  - Errores: {len(errors)}")
                for err in errors[:3]:
                    lines.append(f"    - `{err.get('tool_name', '?')}`: {(err.get('error') or '?')[:80]}")
        lines.append("")

    # C.3 — Tarjeta "Conversaciones recuperables": sesiones harvesteadas que
    # llevan conversation_ref (contrato C.2) y pueden reabrirse con `causadb
    # recover`. Sin resolver blobs, sin leer fuentes (Art. V): usa el state de
    # ReplayEngine ya computado. Disjunta de "Sesiones Recientes" (MAJOR-7):
    # ahí van resúmenes de turnos/tokens; acá tool/session/locator/estado.
    conversations = data.get("conversations_recoverable", {})
    if conversations:
        lines.append("## Conversaciones recuperables\n")
        lines.append("Sesiones con transcript localizable (`conversation_ref`); "
                     "reabrilas con `causadb recover`:\n")
        for sid, conv in sorted(
            conversations.items(), key=lambda kv: kv[1].get("last_timestamp", "")
        )[-10:]:
            ref = conv.get("conversation_ref") or {}
            provider = ref.get("provider") or conv.get("source", "?")
            kind = ref.get("locator_kind") or "?"
            locator = ref.get("locator") or "?"
            status = ref.get("confidence") or "?"
            lines.append(f"- `{provider}` `{sid}` — {kind}/{locator} ({status})")
        lines.append("")

    # Technical state (from resume)
    resume = data.get("resume", {})
    lines.append("## Estado Técnico\n")
    lines.append(f"- **Eventos totales:** {resume.get('events_count', 'N/A')}")
    lines.append(f"- **Último timestamp:** {resume.get('last_timestamp', 'N/A')}")
    unique_count = resume.get("unique_files_count", "N/A")
    event_count = resume.get("files_modified", "N/A")
    lines.append(f"- **Archivos afectados:** {unique_count} ({event_count} eventos)")
    unique_files = resume.get("unique_files", [])
    if unique_files:
        for fp in unique_files[:20]:
            lines.append(f"  - `{fp}`")
        if resume.get("unique_files_truncated"):
            lines.append(f"  - *... y más (ver causadb_query)*")
    commands_count = resume.get("unique_commands_count", 0)
    if commands_count > 0:
        lines.append(f"- **Comandos ejecutados:** {commands_count} comandos únicos")
    lines.append(f"- **LLM invocations:** {resume.get('llm_invocations', 0)}")
    lines.append(f"- **Reasoning steps:** {resume.get('reasoning_steps', 0)}")

    # F1 (M2) — Gap 2: Resumen de entrada = última SESSION_SUMMARY del
    # ledger (``resume.entry_summary``, generado por ``generate_resume``).
    # Solo se renderiza si no es None (anti-teatro Art. IX: nada si vacío).
    entry_summary = resume.get("entry_summary")
    if entry_summary:
        lines.append("## Resumen de entrada\n")
        lines.append(f"- **Tool:** {entry_summary.get('tool', 'unknown')}")
        lines.append(f"- **Session ID:** {entry_summary.get('session_id', 'unknown')}")
        lines.append(f"- **Turnos:** {entry_summary.get('turn_count', 0)}")
        summary_lines = entry_summary.get("summary_lines", [])
        if summary_lines:
            lines.append("- **Líneas de resumen:**")
            for sl in summary_lines[:5]:
                lines.append(f"  - {sl}")
        lines.append("")

    # R.4.1 — OCB granular (memoria de corto plazo).
    # Tabla metadata (preferida) o lista IDs (compat) + magnitud (total_partitions).
    preloaded = resume.get("preloaded_partitions", []) or []
    preloaded_metadata = resume.get("preloaded_metadata") or []
    total_partitions = resume.get("total_partitions", 0)
    session_type = resume.get("session_type", "first_run")

    lines.append("## OCB — memoria granular\n")

    if total_partitions > 0:
        lines.append(f"- **Particiones totales:** {total_partitions}\n")


    if session_type == "first_run":
        lines.append("- Primera sesión: el OCB se poblará automáticamente con esta sesión.\n")
    elif preloaded_metadata and isinstance(preloaded_metadata, list):
        # Render table
        lines.append("| # | Partición | Eventos | Rango |")
        lines.append("|---|-----------|---------|-------|")
        for i, item in enumerate(preloaded_metadata, 1):
            pid = item.get("id", "?")
            count = item.get("event_count", 0)
            first = item.get("first_timestamp", "?")
            last = item.get("last_timestamp", "?")
            # Format range: truncar a 16 chars (YYYY-MM-DDTHH:MM) + "Z"
            rango = f"{first[:16]}Z → {last[:16]}Z" if first and last else "?"
            lines.append(f"| {i} | `{pid}` | {count} | {rango} |")
        lines.append("")
    elif preloaded:
        # Fallback list (backward-compat)
        for item in preloaded:
            pid = item.get("id") if isinstance(item, dict) else item
            lines.append(f"- `{pid}`")
        lines.append("")
    else:
        # Warning rebuild (preservar)
        lines.append(
            f"⚠️ OCB vacío — correr `causadb ocb rebuild --ledger "
            f"{data.get('ledger_path', '')}` para retroalimentar"
        )
        lines.append("")

    # Score section
    score = data.get("score")
    if score:
        lines.append("## Score de Productividad\n")
        lines.append(f"- **Global:** {score.get('overall_score', 'N/A')}/100")
        lines.append(f"- **Churn:** {score.get('churn_score', 'N/A')}/100")
        lines.append(f"- **Waste:** {score.get('waste_score', 'N/A')}/100")
        lines.append(f"- **Survival:** {score.get('survival_score', 'N/A')}/100")
        score_warnings = score.get("warnings", [])
        if score_warnings:
            lines.append(f"- **Warnings:** {', '.join(score_warnings)}")
        lines.append("")

    # Daemon status section
    ds = data.get("daemon_status")
    if ds:
        lines.append("## Estado del Daemon\n")
        for name in ("vigilante", "mcp_proxy", "proxy_server"):
            running = ds.get(name, False)
            icon = "✅" if running else "❌"
            lines.append(f"- {icon} **{name}:** {'activo' if running else 'detenido'}")
        lines.append("")

    # Sync status section (only if configured)
    sync = data.get("sync")
    if sync and sync.get("configured"):
        lines.append("## Estado de Sincronización\n")
        lines.append(f"- **Hub:** {sync.get('hub_url', '')}")
        lines.append(f"- **Última sincronización:** evento #{sync.get('last_synced_seq', 0)}")
        lines.append(f"- **Intervalo:** cada {sync.get('sync_interval_minutes', 60)} minutos")
        lines.append("")

    # Q.3 — Actividad reciente del agente (intención, complemento de REGLA 1).
    # Anti-teatro (Art. IX): la sección SOLO aparece si hay contenido real.
    agent_recent = data.get("agent_recent_activity", [])
    if agent_recent:
        lines.append("## Actividad reciente del agente\n")
        for item in agent_recent:
            if item.get("kind") == "decision":
                ts = item.get("timestamp", "?")[:19]
                dt = item.get("decision_type") or "?"
                imp = item.get("impact") or "?"
                lines.append(f"- `[{ts}]` **[{dt}/{imp}]** {item.get('reasoning', '')}")
            else:
                ts = item.get("timestamp", "?")[:19]
                lines.append(f"- `[{ts}]` archivo: `{item.get('path', '?')}`")
        lines.append("")

    # Governance decisions
    decisions = data.get("governance_decisions", [])
    capa0 = [d for d in decisions if d.get("origin") == "distill"]
    capa1 = [d for d in decisions if d.get("origin") == "agent"]
    lines.append("## Decisiones de Gobernanza\n")
    lines.append(f"**Capa 0 (Framework):** {len(capa0)} decisiones automáticas")
    lines.append(f"**Capa 1 (Agente):** {len(capa1)} decisiones explícitas\n")

    active = [d for d in decisions if d.get("current_status") in {"proposed", "in_progress"}]
    historical = [d for d in decisions if d.get("current_status") in {"done", "superseded", "rejected"}]

    def _render_decision(d):
        dt = d.get("decision_type") or "?"
        imp = d.get("impact") or "?"
        reason = (d.get("reasoning") or "?")[:200]
        origin = d.get("origin") or "?"
        return f"- `[{origin}]` **[{dt}/{imp}]** {reason}"

    if active:
        lines.append("**Decisiones activas:**\n")
        for d in active:
            lines.append(_render_decision(d))
        lines.append("")

    if historical:
        lines.append("**Decisiones historicas:**\n")
        for d in historical:
            lines.append(_render_decision(d))
        lines.append("")

    if not decisions:
        lines.append("*No hay decisiones de gobernanza registradas.*\n")
    else:
        lines.append("")

    # Skills disponibles — usar los ya filtrados del resume (Deuda #25)
    resume_data = data.get("resume", {})
    all_skills = resume_data.get("relevant_skills", [])

    if all_skills:
        lines.append("## Skills disponibles\n")
        lines.append("| Tipo | Nombre | Tokens | Confidence |")
        lines.append("|------|--------|--------|-----------|")
        for skill in all_skills:
            stype = (skill.get("skill_type") or "?")[:15]
            sname = (skill.get("skill_name") or "?")[:40]
            tokens = skill.get("token_count", "?")
            conf = f"{skill.get('confidence', 0.0):.2f}"
            lines.append(f"| {stype} | {sname} | {tokens} | {conf} |")
        lines.append("")

        # Render full file_tree skill if present
        file_tree_skills = [s for s in all_skills if s.get("skill_type") == "file_tree"]
        if file_tree_skills:
            ft = file_tree_skills[0]
            lines.append(f"### {ft.get('skill_name', 'file_tree')} ({ft.get('skill_type', '?')})\n")
            lines.append("```")
            lines.append(_render_file_tree_skill(ft.get("content", "")))
            lines.append("```")
            lines.append("")


    # Tool instructions
    tools = data.get("tools", [])
    if tools:
        lines.append("## Tools Disponibles\n")
        for tool in tools:
            lines.append(f"### `{tool['name']}`")
            lines.append(f"{tool['description']}")
            lines.append("")
            lines.append("**Uso:**")
            lines.append(f"```\n{tool['usage']}\n```")
            lines.append("")

    # REVIVE.0 — Custom Events section
    custom_events = data.get("custom_events", [])
    if custom_events:
        lines.append("## Custom Events\n")
        for ce in custom_events:
            event_type = ce.get("event_type", "?")
            event_id = ce.get("event_id", "?")
            timestamp = ce.get("timestamp", "?")
            payload = ce.get("payload", {})
            # Summary of first 3 payload fields (anti-teatro: no omitir)
            payload_items = list(payload.items())[:3]
            payload_summary = ", ".join(f"{k}: {v}" for k, v in payload_items)
            lines.append(
                f"- `{event_type}` (`{event_id}`) {timestamp}"
                f" — {payload_summary}"
            )
        lines.append("")

    # BIT-14.8 — Drill-down instructions (conditional)
    resume = data.get("resume", {})
    observations = data.get("observations", [])
    drill_down = _generate_drill_down_instructions(resume, observations)
    if drill_down:
        lines.append("## Para profundizar\n")
        lines.append("*Si necesitás más detalle del que muestra este resumen, "
                     "usá estas tools:*\n")
        for line in drill_down:
            lines.append(line)
        lines.append("")

    # R.1.5 — Observaciones pendientes (solo las que no están resueltas).
    # Anti-teatro artículo IX: la sección SOLO aparece si hay al menos una
    # observación pendiente (resolved_reason is None). Si todas están
    # resueltas o no hay observaciones, la sección no se renderiza.
    pending_observations = [
        o for o in observations if o.get("resolved_reason") is None
    ]
    if pending_observations:
        lines.append("## Observaciones pendientes\n")
        for obs in pending_observations:
            fp = obs.get("file_path", "unknown")
            ln = obs.get("line_number")
            desc = obs.get("description", "")
            sev = obs.get("severity", "unknown")
            # BIT-CHR.34 — observaciones de browser (file_path="unknown") no
            # tienen línea atribuible: se muestran por url/title si existen.
            if fp == "unknown" and obs.get("url"):
                display = obs["url"]
                if obs.get("title"):
                    display = f"{display} — {obs['title']}"
                lines.append(f"- {display} [{sev}]")
            else:
                lines.append(f"- `{fp}:{ln}` — {desc} [{sev}]")
        lines.append("")

    # R.4.3 — Orden de reconstrucción (escalera barato→caro). Informativa,
    # siempre presente: revive es el nivel 1 de 4, no el único paso.
    lines.append("## Orden de reconstrucción\n")
    lines.append("Jerarquía barato → caro para entender el estado del proyecto:\n")
    lines.append("1. **revive** — el markdown que estás leyendo (bootstrap context).")
    lines.append("2. **OCB** — memoria granular de corto plazo, precargada arriba. "
                 "Detalle on-demand con `causadb_ocb_load_partition(partition_id)`.")
    lines.append("3. **causadb_query** — filtrado puntual de eventos del ledger por "
                 "tipo, ctx_id, texto o tiempo.")
    lines.append("4. **causadb_replay** — reconstrucción COMPLETA del estado desde "
                 "el ledger (uso pesado, solo si los niveles 1-3 no alcanzan).")
    lines.append("")

    return "\n".join(lines)


# ============================================================================
# CLI handler
# ============================================================================

def cmd_revive(args) -> Tuple[int, str]:
    """Handle ``causadb revive`` — generate volatile revival context."""
    try:
        from causadb._workspace import (
            resolve_ledger,
            get_last_workspace,
            record_last_workspace,
            NoWorkspaceError,
        )
        if getattr(args, "last", False):
            ledger_path = get_last_workspace()
            if ledger_path is None:
                return (1, json.dumps({
                    "error": (
                        "No last workspace found. Run `causadb revive` inside a "
                        "project or `causadb init <path>` to register one."
                    )
                }))
            record_last_workspace(ledger_path)
        else:
            ledger_path = resolve_ledger(
                getattr(args, "ledger", None), fallback_last=True
            )
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))

    return _run_revive(
        ledger_path=ledger_path,
        output_format=getattr(args, "format", "markdown"),
        max_decisions=getattr(args, "decisions", 10),
        write_path=getattr(args, "write", None),
    )


def _run_revive(
    ledger_path: str,
    output_format: str = "markdown",
    max_decisions: int = 10,
    write_path: Optional[str] = None,
) -> Tuple[int, str]:
    """Internal revive pipeline.

    Args:
        ledger_path: Absolute path to the ledger.
        output_format: "markdown" or "json".
        max_decisions: Max governance decisions to include.
        write_path: Optional file path to write output.

    Returns:
        (exit_code, output_string).

    C2 (cap de bytes): el output del step 7 se capa a
    ``MAX_REVIVE_BYTES`` (env ``CAUSADB_MAX_REVIVE_BYTES``). Si lo excede,
    se recortan ``governance_decisions`` desde el final (la lista ya es
    newest-first) y se re-renderiza, con aviso al final (markdown: línea
    ``[...] +N decisiones omitidas...``; json: key ``truncated_notice``).
    El ``write_path`` (step 6) escribe el output COMPLETO sin cap — el cap
    aplica SOLO al return.

    R4 — Política blob faltante: errores de blob faltante (BlobNotFoundError/
    FileNotFoundError) NO matan revive. Se capturan, se registran en
    ``degraded_errors``, y revive continúa con datos de fallback. El output
    incluye ``degraded=True`` + ``degraded_detail`` (JSON) o banner
    "BLOBS FALTANTES" (markdown). Solo errores NO-blob propagan rc=1.
    """
    from causadb._blob_store import BlobNotFoundError

    # Track blob-related errors for degraded mode
    degraded_errors = []

    # Record the ledger as the last workspace (best-effort) so a future
    # `causadb revive --last` finds it. Covers CLI and MCP revive paths.
    try:
        from causadb._workspace import record_last_workspace
        record_last_workspace(ledger_path)
    except Exception:
        pass

    # Step 1: Promote decisions to governance (Capa 0)
    try:
        promoted = _promote_decisions_to_governance(ledger_path)
        detected = _detect_destructive_commands(ledger_path)
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        promoted = 0
        detected = 0
    except Exception as e:
        return (1, json.dumps({"error": f"Promotion failed: {e}"}))

    # Step 2 — ÚNICA reconstruct dentro de _run_revive (anti-teatro).
    try:
        engine = ReplayEngine(ledger_path)
        replay_state = engine.reconstruct_state()
        observations = replay_state.get("observations", [])
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        observations = []
        replay_state = {}
    except Exception:
        observations = []
        replay_state = {}

    # Step 3: Generate resume (reusing replay_state to avoid double replay)
    try:
        from causadb.cli._cmd_resume import generate_resume
        resume_data = generate_resume(ledger_path, state=replay_state)
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        resume_data = {}
    except Exception:
        resume_data = {}

    # Step 3.5: Passive closure distillation for abrupt_close sessions
    # If resume indicates abrupt_close with a last_session_id, distill a
    # GOVERNANCE_DECISION for the closure (idempotent: writes endorsement
    # if already exists).
    try:
        session_type = resume_data.get("session_type")
        last_session_id = resume_data.get("last_session_id")
        if session_type == "abrupt_close" and last_session_id:
            from causadb._decision_distill import distill_closure_decision
            distill_closure_decision(ledger_path, last_session_id)
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
    except Exception:
        # Silently ignore closure distillation errors (degradation)
        pass

    # Decisions reusando replay_state (sin nueva reconstruct).
    try:
        decisions = _generate_governance_decisions(
            ledger_path, max_decisions, state=replay_state
        )
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        decisions = []
    except Exception:
        decisions = []

    # Step 4: Generate tool instructions
    tools = _generate_tool_instructions()

    # Step 4.5: Add Score, daemon status, and sync status
    try:
        from causadb._score import compute_score
        score_data = compute_score(ledger_path)
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        score_data = None
    except Exception:
        score_data = None

    # Q.3 — Actividad reciente del agente (Capa 1, sin replay completo).
    try:
        agent_recent_activity = _generate_agent_recent_activity(ledger_path)
    except (BlobNotFoundError, FileNotFoundError) as e:
        degraded_errors.append(str(e))
        agent_recent_activity = []
    except Exception:
        agent_recent_activity = []

    try:
        from causadb._daemon import is_running
        daemon_status = {
            "vigilante": is_running("vigilante"),
            "mcp_proxy": is_running("mcp_proxy"),
            "proxy_server": is_running("proxy_server"),
        }
    except Exception:
        daemon_status = {}

    try:
        from causadb._sync import SyncEngine
        sync_engine = SyncEngine(ledger_path, os.path.dirname(ledger_path))
        sync_cfg = sync_engine.get_config()
        sync_data = {
            "configured": bool(sync_cfg.get("hub_url")),
            "hub_url": sync_cfg.get("hub_url", ""),
            "last_synced_seq": sync_cfg.get("last_synced_seq", 0),
            "sync_interval_minutes": sync_cfg.get("interval_minutes", 60),
        }
    except Exception:
        sync_data = None

    # Step 5: Build output data
    data = {
        "ledger_path": ledger_path,
        "generated_at": datetime.now().isoformat(),
        "resume": resume_data,
        "governance_decisions": decisions,
        "agent_recent_activity": agent_recent_activity,
        "observations": observations,
        "project_snapshots": replay_state.get("project_snapshots", []),
        "custom_events": replay_state.get("custom_events", []),
        "session_summaries": replay_state.get("session_summaries", []),
        "conversations_recoverable": replay_state.get("conversations_recoverable", {}),
        "tools": tools,
        "promotion_stats": {
            "promoted": promoted,
            "destructive_detected": detected,
        },
        "score": score_data,
        "daemon_status": daemon_status,
        "sync": sync_data,
        "skills_precomputed": resume_data.get("relevant_skills", []),
    }

    # Add degraded flag if any blob errors occurred
    if degraded_errors:
        data["degraded"] = True
        data["degraded_detail"] = {
            "error_count": len(degraded_errors),
            "errors": degraded_errors,
        }
    else:
        data["degraded"] = False

    # Step 6: Optionally write to file
    if write_path:
        try:
            output_content = (
                _generate_revive_markdown(data)
                if output_format == "markdown"
                else json.dumps(data, indent=2, default=str)
            )
            os.makedirs(os.path.dirname(os.path.abspath(write_path)), exist_ok=True)
            with open(write_path, "w") as f:
                f.write(output_content)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            return (1, json.dumps({"error": f"Write failed: {e}"}))

    # Step 7: Return output (C2 — cap de bytes con trim a nivel de datos).
    # El write_path (step 6, ANTES) escribe el output COMPLETO sin cap; el
    # cap aplica SOLO al return.
    max_bytes = _max_revive_bytes()
    if output_format == "markdown":
        output = _generate_revive_markdown(data)
        truncated = len(output) > max_bytes
        dropped = 0
        while len(output) > max_bytes and len(data["governance_decisions"]) > 1:
            # Trim a nivel de DATOS: recorta las decisiones más viejas
            # (la lista ya es newest-first, _generate_governance_decisions).
            data["governance_decisions"] = data["governance_decisions"][:-1]
            dropped += 1
            output = _generate_revive_markdown(data)
        if truncated:
            output += (
                f"\n\n> [...] +{dropped} decisiones omitidas por tamaño "
                f"(Máx {max_bytes // 1024}KB). Usá causadb_query "
                f"event_type=GOVERNANCE_DECISION para verlas.\n"
            )
        # R4: rc=0 si solo hay errores de blob (degraded), rc=1 solo para errores duros
        return (0, output)

    output = json.dumps(data, indent=2, default=str)
    truncated = len(output) > max_bytes
    dropped = 0
    while len(output) > max_bytes and len(data["governance_decisions"]) > 1:
        data["governance_decisions"] = data["governance_decisions"][:-1]
        dropped += 1
        output = json.dumps(data, indent=2, default=str)
    if truncated:
        data["truncated_notice"] = (
            f"+{dropped} decisiones omitidas por tamaño "
            f"(Máx {max_bytes // 1024}KB). Usá causadb_query "
            f"event_type=GOVERNANCE_DECISION para verlas."
        )
        output = json.dumps(data, indent=2, default=str)
    # R4: rc=0 si solo hay errores de blob (degraded), rc=1 solo para errores duros
    return (0, output)
