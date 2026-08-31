"""H8.5 — Agent Activity Report: consolida qué hizo un agente en el período
del ledger consumiendo SOLO la proyección del ReplayEngine ya computada
(Art. V — nunca lee stores/fuentes). Función pura, agnóstica de agente:
no muta ``state``, no escribe al ledger y no lanza excepciones ante datos
raros (``.get()`` con defaults robustos).
"""

from typing import Any, Dict, List, Optional

from causadb._cost_rollup import CostRollup

_CATEGORIES = [
    "files_modified",
    "commands_run",
    "commits_made",
    "api_activity",
    "llm_invocations",
    "reasoning_steps",
    "cost_accounted",
]

_SUCCESS_STATUSES = {"success", "completed", "ok"}
_FAILURE_STATUSES = {"failed", "timeout", "cancelled", "error"}

_SESSION_NOTE = (
    "nota: files_modified/commands_run/commits_made/llm_invocations NO "
    "filtran por session_id porque la proyección no expone session; el "
    "filtro --session aplica solo a api_attempts (hermes_session_id), "
    "reasoning_steps (session_id si existe) y cost_accounted (session_id "
    "si existe)"
)


def _as_number(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _in_time_range(timestamp, from_time, to_time):
    if from_time is None and to_time is None:
        return True
    if not isinstance(timestamp, str) or not timestamp:
        return True
    if from_time is not None and timestamp < from_time:
        return False
    if to_time is not None and timestamp > to_time:
        return False
    return True


def _matches_session(entry, key, session_id):
    if session_id is None:
        return True
    return entry.get(key) == session_id


def _unique_non_empty(values) -> List[str]:
    seen: List[str] = []
    for v in values:
        if v is not None and v != "":
            s = str(v)
            if s not in seen:
                seen.append(s)
    return seen


def _normalize_events(events):
    """Aplana entradas raw del ledger ({hash, prev_hash, event:{...}}) al
    formato plano que espera ``CostRollup.validate_hermes_consistency``
    ({type, ...payload}); eventos ya planos pasan tal cual."""
    flat = []
    for ev in events or []:
        if isinstance(ev, dict) and isinstance(ev.get("event"), dict):
            inner = ev["event"]
            merged = dict(inner.get("payload", {}) or {})
            merged["type"] = inner.get("event_type") or inner.get("type")
            flat.append(merged)
        else:
            flat.append(ev)
    return flat


def _sources_observed(state) -> List[str]:
    """Valores únicos no vacíos de entry.get("source") más
    state.meta.sources; si no hay ninguno → ["unknown"] (Art. V)."""
    sources = []
    meta = state.get("meta") or {}
    meta_sources = meta.get("sources")
    if isinstance(meta_sources, list):
        sources.extend(meta_sources)
    elif isinstance(meta_sources, str) and meta_sources:
        sources.append(meta_sources)
    for key in (
        "files_modified", "commands_run", "commits_made", "api_attempts",
        "llm_invocations", "reasoning_steps", "cost_accounted",
    ):
        for entry in state.get(key, []):
            src = entry.get("source")
            if src is not None and src != "":
                sources.append(src)
    observed = _unique_non_empty(sources)
    return observed or ["unknown"]


def build_agent_activity_report(
    state: Dict[str, Any],
    session_id: Optional[str] = None,
    from_time: Optional[str] = None,
    to_time: Optional[str] = None,
    events: Optional[List[dict]] = None,
) -> dict:
    """Reporte consolidado de actividad de agente desde la proyección.

    ``from_time``/``to_time`` (ISO 8601, comparación lexicográfica
    inclusiva) aplican a TODAS las categorías. ``session_id`` aplica SOLO
    donde la proyección expone session (api_attempts → ``hermes_session_id``,
    reasoning_steps/cost_accounted → ``session_id``); las categorías sin
    session quedan globales con nota en ``filter_notes``. ``cost_consistency``
    solo se computa si el caller provee ``events`` (raw del ledger).
    """
    state = state or {}

    files = [
        e for e in state.get("files_modified", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
    ]
    by_action: Dict[str, int] = {}
    paths: List[Any] = []
    for e in files:
        action = e.get("action") or "unknown"
        by_action[action] = by_action.get(action, 0) + 1
        paths.append(e.get("path"))

    commands = [
        e for e in state.get("commands_run", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
    ]
    cmd_failures = 0
    cmd_list: List[Any] = []
    for e in commands:
        cmd_list.append(e.get("command"))
        ec_int = _as_int(e.get("exit_code"), default=None)
        if ec_int is not None and ec_int != 0:
            cmd_failures += 1

    commits = [
        e for e in state.get("commits_made", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
    ]
    commit_msgs: List[Any] = []
    for e in commits:
        msg = e.get("message")
        if msg is None or msg == "":
            msg = e.get("commit_hash")
        commit_msgs.append(msg)

    api_entries = [
        e for e in state.get("api_attempts", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
        and _matches_session(e, "hermes_session_id", session_id)
    ]
    api_success = 0
    api_failed = 0
    api_tokens_in = 0
    api_tokens_out = 0
    api_cost = 0.0
    api_by_model: Dict[str, int] = {}
    for e in api_entries:
        status = e.get("status") or "unknown"
        if status in _SUCCESS_STATUSES:
            api_success += 1
        elif status in _FAILURE_STATUSES:
            api_failed += 1
        api_tokens_in += _as_int(e.get("tokens_in"))
        api_tokens_out += _as_int(e.get("tokens_out"))
        api_cost += float(_as_number(e.get("cost_usd"), 0.0))
        model = e.get("model") or "unknown"
        api_by_model[model] = api_by_model.get(model, 0) + 1

    llm_entries = [
        e for e in state.get("llm_invocations", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
    ]
    llm_tokens = 0
    llm_by_model: Dict[str, int] = {}
    for e in llm_entries:
        llm_tokens += _as_int(e.get("response_tokens"))
        model = e.get("model") or "unknown"
        llm_by_model[model] = llm_by_model.get(model, 0) + 1

    reasoning_entries = [
        e for e in state.get("reasoning_steps", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
        and _matches_session(e, "session_id", session_id)
    ]
    rs_by_kind: Dict[str, int] = {}
    for e in reasoning_entries:
        kind = e.get("kind") or e.get("step_type") or "unknown"
        rs_by_kind[kind] = rs_by_kind.get(kind, 0) + 1

    cost_entries = [
        e for e in state.get("cost_accounted", [])
        if _in_time_range(e.get("timestamp"), from_time, to_time)
        and _matches_session(e, "session_id", session_id)
    ]
    ca_tokens_in = 0
    ca_tokens_out = 0
    ca_cost = 0.0
    ca_currency = "USD"
    for e in cost_entries:
        ca_tokens_in += _as_int(e.get("tokens_in"))
        ca_tokens_out += _as_int(e.get("tokens_out"))
        ca_cost += float(_as_number(e.get("cost"), 0.0))
        cur = e.get("currency")
        if isinstance(cur, str) and cur:
            ca_currency = cur

    categories = {
        "files_modified": {"count": len(files), "by_action": by_action, "paths": paths},
        "commands_run": {"count": len(commands), "with_failures": cmd_failures, "commands": cmd_list},
        "commits_made": {"count": len(commits), "commits": commit_msgs},
        "api_activity": {
            "count": len(api_entries),
            "success": api_success,
            "failed": api_failed,
            "tokens_in": api_tokens_in,
            "tokens_out": api_tokens_out,
            "cost_usd": api_cost,
            "by_model": api_by_model,
        },
        "llm_invocations": {
            "count": len(llm_entries),
            "response_tokens": llm_tokens,
            "by_model": llm_by_model,
        },
        "reasoning_steps": {"count": len(reasoning_entries), "by_kind": rs_by_kind},
        "cost_accounted": {
            "count": len(cost_entries),
            "tokens_in": ca_tokens_in,
            "tokens_out": ca_tokens_out,
            "cost": ca_cost,
            "currency": ca_currency,
        },
    }

    total_events = sum(cat["count"] for cat in categories.values())
    unobserved = [cat for cat in _CATEGORIES if categories[cat]["count"] == 0]

    cost_consistency = None
    if events is not None:
        cost_consistency = CostRollup.validate_hermes_consistency(_normalize_events(events))

    filter_notes: List[str] = []
    if session_id is not None:
        filter_notes.append(_SESSION_NOTE)

    return {
        "agent_activity_report": {
            "filters": {
                "session_id": session_id,
                "from_time": from_time,
                "to_time": to_time,
            },
            "summary": {
                "total_events_considered": total_events,
                "agents_sources_observed": _sources_observed(state),
            },
            **categories,
            "cost_consistency": cost_consistency,
            "unobserved": unobserved,
            "filter_notes": filter_notes,
        }
    }
