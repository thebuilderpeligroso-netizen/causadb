import json
import os
from typing import Optional, Dict, Any
from datetime import datetime
from causadb._config import CausaDBConfig
from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator, ReplayIntegrityError

class ReplayEngine:
    def __init__(self, ledger_path: str, config: Optional[CausaDBConfig] = None):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.config = config or CausaDBConfig(ledger_path=ledger_path)
        self.validator = LedgerValidator(ledger_path)
        self.reader = LedgerReader(ledger_path)

    def _initial_state(self) -> Dict[str, Any]:
        return {
            "events_applied": 0,
            "last_hash": "GENESIS",
            "timestamp": None,
            "files_modified": [],
            "custom_events": [],
            "commands_run": [],
            "commits_made": [],
            "configs_changed": [],
            "context": {},
            "tools_called": [],
            "queries_executed": [],
            "sessions": [],
            "mutations_applied": [],
            "mutations_reverted": [],
            "system_boots": [],
            "checkpoints": [],
            "llm_invocations": [],
            "cost_accounted": [],
            "retrievals_done": [],
            "memory_ops": [],
            "agent_handoffs": [],
            "human_feedback": [],
            "stale_event_ids": [],
            "events_index": {},
            "sandbox_violations": [],
            "sandbox_mutations": [],
            "reasoning_steps": [],
            "context_compactions": [],
            "stream_interrupts": [],
            "skills": [],
            "scores_recorded": [],
            "governance_decisions": [],
            "project_snapshots": [],
            "observations": [],
            "chronicle_entries": [],
            "session_summaries": [],
            "conversations_recoverable": {},
            "delegations": [],
            "api_attempts": [],
        }

    def apply(self, event_entry: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
        event_data = event_entry["event"]
        event_type = event_data["event_type"]
        payload = event_data.get("payload", {})
        
        from causadb._event_registry import is_builtin, is_registered
        if event_type == "DELEGATION":
            return self._apply_delegation(event_entry=event_entry, event_data=event_data, payload=payload, state=state)
        if not is_builtin(event_type) and is_registered(event_type):
            return self._apply_custom_event(event_type=event_type, payload=payload, event_data=event_data, state=state)
        
        state["events_applied"] += 1
        state["last_hash"] = event_entry["hash"]
        state["timestamp"] = event_data["timestamp"]
        
        # Poblar events_index para lookup de parent_event_id (stale propagation)
        state["events_index"][event_data["event_id"]] = {
            "parent_event_id": event_data.get("parent_event_id"),
            "event_type": event_type,
        }

        # C.3 — Conservar conversation_ref en state (contrato C.2). Cualquier
        # evento de sesión harvesteado (TOOL_CALLED, LLM_INVOKED, ...) puede
        # llevar `conversation_ref` en payload (proyectado por
        # _event_from_raw). Se deduplica por session_id: la tarjeta de revive
        # muestra la última actividad de cada conversación recuperable.
        conv_ref = payload.get("conversation_ref")
        if conv_ref is not None:
            sid = payload.get("session_id") or conv_ref.get("session_id")
            if sid:
                state["conversations_recoverable"][sid] = {
                    "session_id": sid,
                    "conversation_ref": conv_ref,
                    "session_locator": payload.get("session_locator"),
                    "source": event_data.get("source", ""),
                    "last_event_type": event_type,
                    "last_timestamp": event_data["timestamp"],
                }
        
        if event_type == "FILE_MODIFIED":
            state["files_modified"].append({
                "path": payload.get("path", "unknown"),
                "action": payload.get("action", "unknown"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "COMMAND_RUN":
            state["commands_run"].append({
                "command": payload.get("command", "unknown"),
                "exit_code": payload.get("exit_code"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "COMMIT_MADE":
            state["commits_made"].append({
                "commit_hash": payload.get("commit_hash", "unknown"),
                "message": payload.get("message"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "CONFIG_CHANGED":
            state["configs_changed"].append({
                "path": payload.get("path", "unknown"),
                "key": payload.get("key"),
                "value": payload.get("value"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "CONTEXT_UPDATED":
            state["context"].update(payload.get("context", {}))
        elif event_type == "TOOL_CALLED":
            state["tools_called"].append({
                "tool_name": payload.get("tool_name", "unknown"),
                "arguments": payload.get("arguments"),
                "result": payload.get("result"),
                "duration_ms": payload.get("duration_ms"),
                "error": payload.get("error"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "DB_QUERY":
            state["queries_executed"].append({
                "query": payload.get("query", "unknown"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "SESSION_STARTED":
            state["sessions"].append({
                "session_id": payload.get("session_id", "unknown"),
                "started_at": event_data["timestamp"],
                "ended_at": None,
                "duration_ms": None,
                "source": event_data.get("source"),
            })
        elif event_type == "SESSION_ENDED":
            sid = payload.get("session_id", "unknown")
            for session in state["sessions"]:
                if session["session_id"] == sid and session["ended_at"] is None:
                    session["ended_at"] = event_data["timestamp"]
                    started = session["started_at"]
                    ended = event_data["timestamp"]
                    try:
                        from datetime import datetime
                        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
                        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                        session["duration_ms"] = int((e - s).total_seconds() * 1000)
                    except (ValueError, AttributeError):
                        session["duration_ms"] = None
                    break
        elif event_type == "MUTATION_APPLIED":
            state["mutations_applied"].append({
                "mutation_id": payload.get("mutation_id", "unknown"),
                "timestamp": event_data["timestamp"],
                "reverted": False,
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            })
        elif event_type == "MUTATION_REVERTED":
            target = payload.get("revert_target_event_id")
            for mutation in state["mutations_applied"]:
                if mutation.get("event_id") == target:
                    mutation["reverted"] = True
                    break
            state["mutations_reverted"].append({
                "revert_target_event_id": target,
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "SYSTEM_BOOT":
            state["system_boots"].append({
                "boot_id": payload.get("boot_id", "unknown"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "CHECKPOINT_CREATED":
            state["checkpoints"].append({
                "checkpoint_id": payload.get("checkpoint_id", "unknown"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, dict):
                state.update(snapshot)
        elif event_type == "LLM_INVOKED":
            state["llm_invocations"].append({
                "model": payload.get("model", "unknown"),
                "prompt": payload.get("prompt"),
                "response_tokens": payload.get("response_tokens"),
                "duration_ms": payload.get("duration_ms"),
                "error": payload.get("error"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "COST_ACCOUNTED":
            state["cost_accounted"].append({
                "model": payload.get("model", "unknown"),
                "tokens_in": payload.get("tokens_in", 0),
                "tokens_out": payload.get("tokens_out", 0),
                "cost": payload.get("cost", 0.0),
                "currency": payload.get("currency", "USD"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "MEMORY_OP":
            state["memory_ops"].append({
                "operation": payload.get("operation", "unknown"),
                "key": payload.get("key", ""),
                "value": payload.get("value"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "RETRIEVAL_DONE":
            state["retrievals_done"].append({
                "query": payload.get("query", ""),
                "chunks": payload.get("chunks", []),
                "scores": payload.get("scores", []),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "AGENT_HANDOFF":
            state["agent_handoffs"].append({
                "from_agent": payload.get("from_agent", "unknown"),
                "to_agent": payload.get("to_agent", "unknown"),
                "trace_id": payload.get("trace_id"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
        elif event_type == "HUMAN_FEEDBACK":
            state["human_feedback"].append({
                "feedback_type": payload.get("feedback_type", "unknown"),
                "target_event_id": payload.get("target_event_id"),
                "reason": payload.get("reason"),
                "score": payload.get("score"),
                "max_score": payload.get("max_score"),
                "comment": payload.get("comment"),
                "original_hash": payload.get("original_hash"),
                "edited_hash": payload.get("edited_hash"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })
            # Stale-downstream propagation: si feedback_type == "edit", marcar
            # el target_event_id y todos sus descendientes (eventos cuyo
            # parent_event_id apunta a un evento ya stale) como stale.
            if payload.get("feedback_type") == "edit":
                target_id = payload.get("target_event_id")
                if target_id and target_id not in state["stale_event_ids"]:
                    state["stale_event_ids"].append(target_id)
                # Propagar recursivamente: cualquier evento cuyo parent_event_id
                # esté en stale_event_ids también se marca stale. Loop hasta
                # converger (no se agregan nuevos).
                changed = True
                while changed:
                    changed = False
                    for eid, meta in state["events_index"].items():
                        parent = meta.get("parent_event_id")
                        if parent in state["stale_event_ids"] and eid not in state["stale_event_ids"]:
                            state["stale_event_ids"].append(eid)
                            changed = True

        elif event_type == "SANDBOX_STATE":
            entry = {
                "mutation_type": payload.get("mutation_type", "unknown"),
                "path_or_resource": payload.get("path_or_resource", "unknown"),
                "sandbox_boundary": payload.get("sandbox_boundary"),
                "violates_boundary": payload.get("violates_boundary", False),
                "process_pid": payload.get("process_pid"),
                "process_name": payload.get("process_name"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            }
            if entry["violates_boundary"]:
                state["sandbox_violations"].append(entry)
            else:
                state["sandbox_mutations"].append(entry)

        elif event_type == "REASONING_STEP":
            state["reasoning_steps"].append({
                "step_type": payload.get("step_type", "unknown"),
                "step_hash": payload.get("step_hash", ""),
                "reasoning_level": payload.get("reasoning_level"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })

        elif event_type == "CONTEXT_COMPACTED":
            pre = payload.get("pre_token_count", 0)
            post = payload.get("post_token_count", 0)
            tokens_lost = payload.get("tokens_lost", pre - post if post <= pre else 0)
            state["context_compactions"].append({
                "pre_token_count": pre,
                "post_token_count": post,
                "tokens_lost": tokens_lost,
                "eviction_policy": payload.get("eviction_policy"),
                "summary_model": payload.get("summary_model"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })

        elif event_type == "STREAM_INTERRUPTED":
            state["stream_interrupts"].append({
                "interrupt_reason": payload.get("interrupt_reason", "unknown"),
                "partial_completion_hash": payload.get("partial_completion_hash", ""),
                "partial_token_count": payload.get("partial_token_count"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })

        elif event_type == "SKILL_CREATED":
            new_skill = {
                "skill_id": payload.get("skill_id", "unknown"),
                "skill_type": payload.get("skill_type", "unknown"),
                "skill_name": payload.get("skill_name", "unknown"),
                "content": payload.get("content", ""),
                "token_count": payload.get("token_count", 0),
                "confidence": payload.get("confidence", 0.0),
                "source_session": payload.get("source_session"),
                "timestamp": event_data["timestamp"],
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            }
            # Dedupe in-place por skill_name (BIT-CHR.103). Ultimo gana (Opcion A).
            name = new_skill["skill_name"]
            state["skills"] = [s for s in state["skills"] if s.get("skill_name") != name]
            state["skills"].append(new_skill)

        elif event_type == "SKILL_PRUNED":
            pruned_id = payload.get("skill_id")
            state["skills"] = [s for s in state["skills"] if s.get("skill_id") != pruned_id]

        elif event_type == "SCORE_RECORDED":
            state["scores_recorded"].append({
                "overall_score": payload.get("overall_score", 0.0),
                "churn_score": payload.get("churn_score", 0.0),
                "waste_score": payload.get("waste_score", 0.0),
                "survival_score": payload.get("survival_score", 0.0),
                "session_id": payload.get("session_id"),
                "weights_used": payload.get("weights_used", {}),
                "correlation_method": payload.get("correlation_method", "timestamp_proximity"),
                "timestamp": event_data["timestamp"],
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            })

        elif event_type == "GOVERNANCE_DECISION":
            state["governance_decisions"].append({
                "event_id": event_data.get("event_id"),
                "reasoning": payload.get("reasoning"),
                "impact": payload.get("impact"),
                "decision_type": payload.get("decision_type"),
                "origin": payload.get("origin"),
                "timestamp": event_data["timestamp"],
                "current_status": "proposed",
                "source": event_data.get("source"),
            })

        elif event_type == "OBSERVATION":
            # R.1.3 — Validar severity (Fall-Closed, independiente del schema
            # validator: el replay debe rechazar severity fuera del enum aunque
            # el evento haya entrado por una vía que bypassa el writer).
            # BIT-CHR.34 — Tolerancia histórica: OBSERVATION de
            # harvester:browser sin severity (payload {url,title,visit_time})
            # se reconstruye como "info" (fingerprint causal por source, no
            # por url). Otros sources sin severity siguen fallando: corrupción
            # genuina.
            severity = payload.get("severity")
            allowed_severities = {"info", "minor", "major", "blocker"}
            if severity is None and event_data.get("source") == "harvester:browser":
                severity = "info"
            if severity not in allowed_severities:
                raise ValueError(
                    f"OBSERVATION event has invalid severity {severity!r}. "
                    f"Allowed: {sorted(allowed_severities)}"
                )
            state["observations"].append({
                "file_path": payload.get("file_path", "unknown"),
                "line_number": payload.get("line_number"),
                "description": payload.get("description", ""),
                "severity": severity,
                "url": payload.get("url"),
                "title": payload.get("title"),
                "resolved_reason": None,
                "event_id": event_data.get("event_id"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })

        elif event_type == "OBSERVATION_RESOLVED":
            # R.1.3 — Patrón cross-event: no appendea un nuevo entry. Busca el
            # OBSERVATION original por event_id == parent_event_id y setea
            # resolved_reason. Fall-Closed: si no encuentra el parent, lanza
            # ValueError (artículo I — ledger monism, no orphan resolutions).
            parent_id = event_data.get("parent_event_id")
            resolved_reason = payload.get("resolved_reason")
            if not parent_id:
                raise ValueError(
                    "OBSERVATION_RESOLVED event requires parent_event_id "
                    "pointing to the original OBSERVATION event"
                )
            found = False
            for obs in state["observations"]:
                if obs.get("event_id") == parent_id:
                    obs["resolved_reason"] = resolved_reason
                    found = True
                    break
            if not found:
                raise ValueError(
                    f"OBSERVATION_RESOLVED event references parent_event_id "
                    f"{parent_id!r} but no matching OBSERVATION entry was "
                    f"found in state['observations']"
                )

        elif event_type == "GOVERNANCE_DECISION_STATUS_CHANGED":
            # R.2.3 — Patrón cross-event: busca la decisión original
            # por event_id == parent_event_id y actualiza current_status.
            parent_id = event_data.get("parent_event_id")
            new_status = payload.get("new_status")
            if not parent_id:
                raise ValueError(
                    "GOVERNANCE_DECISION_STATUS_CHANGED event requires "
                    "parent_event_id pointing to the original "
                    "GOVERNANCE_DECISION event"
                )
            found = False
            for gd in state["governance_decisions"]:
                if gd.get("event_id") == parent_id:
                    gd["current_status"] = new_status
                    found = True
                    break
            if not found:
                raise ValueError(
                    f"GOVERNANCE_DECISION_STATUS_CHANGED event references "
                    f"parent_event_id {parent_id!r} but no matching "
                    f"GOVERNANCE_DECISION entry was found in "
                    f"state['governance_decisions']"
                )

        elif event_type == "PROJECT_SNAPSHOT":
            state["project_snapshots"].append({
                "total_events": payload.get("total_events"),
                "total_tests": payload.get("total_tests"),
                "fases_completadas": payload.get("fases_completadas"),
                "bloqueantes_resueltos": payload.get("bloqueantes_resueltos"),
                "notas": payload.get("notas"),
                "timestamp": event_data["timestamp"],
                "source": event_data.get("source"),
            })

        elif event_type == "CHRONICLE_ENTRY":
            state["chronicle_entries"].append({
                "bit_id": payload.get("bit_id", ""),
                "title": payload.get("title", ""),
                "date": payload.get("date", ""),
                "maker": payload.get("maker", ""),
                "checker": payload.get("checker", ""),
                "summary": payload.get("summary", ""),
                "files_touched": payload.get("files_touched", []),
                "timestamp": event_data["timestamp"],
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            })

        elif event_type == "SESSION_SUMMARY":
            state["session_summaries"].append({
                "tool": payload.get("tool", "unknown"),
                "session_id": payload.get("session_id", "unknown"),
                "turn_count": payload.get("turn_count", 0),
                "summary_lines": payload.get("summary_lines", []),
                "decisions": payload.get("decisions", []),
                "errors": payload.get("errors", []),
                "files_touched": payload.get("files_touched", []),
                "tokens_used": payload.get("tokens_used", 0),
                "duration_s": payload.get("duration_s", 0),
                "timestamp": event_data["timestamp"],
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            })
        elif event_type == "API_ATTEMPT":
            state["api_attempts"].append({
                "hermes_session_id": payload.get("hermes_session_id"),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "mode": payload.get("mode"),
                "status": payload.get("status", "unknown"),
                "request_ref": payload.get("request_ref"),
                "tokens_in": payload.get("tokens_in", 0),
                "tokens_out": payload.get("tokens_out", 0),
                "cost_usd": payload.get("cost_usd", 0.0),
                "base_url": payload.get("base_url"),
                "correlation_id": payload.get("correlation_id"),
                "log_thread_id": payload.get("log_thread_id"),
                "api_call_count": payload.get("api_call_count"),
                "cache_read_tokens": payload.get("cache_read_tokens"),
                "cache_write_tokens": payload.get("cache_write_tokens"),
                # H2.2 — nombres de schema del API_ATTEMPT (los *_tokens de
                # arriba se conservan por back-compat con eventos pre-H2.2).
                "cache_read": payload.get("cache_read"),
                "cache_write": payload.get("cache_write"),
                "reasoning_tokens": payload.get("reasoning_tokens"),
                "latency_ms": payload.get("latency_ms"),
                "error": payload.get("error"),
                "parent_event_id": payload.get("parent_event_id"),
                "source_version": payload.get("source_version"),
                "schema_version": payload.get("schema_version"),
                "timestamp": event_data["timestamp"],
                "event_id": event_data.get("event_id"),
                "source": event_data.get("source"),
            })

        return state

    def _apply_custom_event(self, event_type, payload, event_data, state):
        state["custom_events"].append({
            "event_type": event_type,
            "payload": dict(payload),
            "event_id": event_data.get("event_id"),
            "timestamp": event_data["timestamp"],
            "source": event_data.get("source"),
        })
        return state

    def _apply_delegation(self, event_entry, event_data, payload, state):
        """Aplica un evento DELEGATION al estado estructurado de delegaciones.

        Mantiene UNA entrada por delegation_id con el estado observado y los
        campos del payload. La resolución del estado final (no-terminal ->
        ``unobserved``) se hace en ``_resolve_delegation_states`` al final del
        replay (H0.2 — Bloqueante #1: lifecycle de DELEGATION).
        """
        state["events_applied"] += 1
        state["last_hash"] = event_entry["hash"]
        state["timestamp"] = event_data["timestamp"]
        state["events_index"][event_data["event_id"]] = {
            "parent_event_id": event_data.get("parent_event_id"),
            "event_type": "DELEGATION",
        }
        delegation_id = payload.get("delegation_id", "unknown")
        entry = {
            "delegation_id": delegation_id,
            "state": payload.get("state"),
            "result_json": payload.get("result_json"),
            "delivery_state": payload.get("delivery_state"),
            "origin_session": payload.get("origin_session"),
            "parent_session_id": payload.get("parent_session_id"),
            "task_json": payload.get("task_json"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "source": event_data.get("source"),
        }
        for i, d in enumerate(state["delegations"]):
            if d["delegation_id"] == delegation_id:
                state["delegations"][i] = entry
                break
        else:
            state["delegations"].append(entry)
        return state

    def _resolve_delegation_states(self, state):
        """Resuelve el estado final de las delegaciones tras el replay.

        La delegación más reciente (última en orden de replay) que quedó en un
        estado no-terminal y no explícitamente ``unknown``/``unobserved`` se
        resuelve a ``unobserved``: arrancó pero nunca confirmó su efecto.
        Determinístico: misma secuencia de eventos -> mismo resultado.
        """
        delegations = state.get("delegations", [])
        if not delegations:
            return
        terminal = {"completed", "failed", "timeout", "cancelled", "interrupted"}
        explicit_kept = {"unknown", "unobserved"}
        last = delegations[-1]
        if last["state"] not in terminal and last["state"] not in explicit_kept:
            last["state"] = "unobserved"

    def reconstruct_state(self, to_time: Optional[str] = None, until_event_id: Optional[str] = None) -> Dict[str, Any]:
        self.validator.validate_or_raise()

        to_dt: Optional[datetime] = None
        if to_time is not None:
            to_dt = datetime.fromisoformat(to_time.replace("Z", "+00:00"))

        state = self._initial_state()
        # GAP-02 — replay parcial por frontera: ``until_event_id`` corta el
        # replay en el PREFIJO DE APPEND (orden del ledger, D1: append-order
        # sobre time-order) hasta el event_id inclusive. Backward-compatible:
        # sin until_event_id → replay completo (como antes).
        if until_event_id is not None:
            entries = self.reader.read_until_entries(until_event_id)
        else:
            entries = self.reader.read_all_entries()
        for entry in entries:
            ts = (entry.get("event") or {}).get("timestamp")
            if to_dt is not None and ts:
                try:
                    entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if entry_dt > to_dt:
                        break
                except (ValueError, TypeError):
                    pass
            state = self.apply(entry, state)
        self._resolve_delegation_states(state)
        return state
