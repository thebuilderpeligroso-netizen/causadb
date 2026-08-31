"""NIST AI RMF compliance report generator (F.7.2).

Produces a report mapping NIST AI RMF 1.0 + GenAI Profile (NIST-AI-600-1)
four functions (Govern, Map, Measure, Manage) to CausaDB evidence.

Function, not class (Article VIII). Falls-Closed: always returns dict,
never raises exception (decision #8).
"""

from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator
from causadb._replay_engine import ReplayEngine
from causadb._sentinel_rules import evaluate_rules


def generate_nist_report(ledger_path: str) -> dict:
    validator = LedgerValidator(ledger_path)
    vr = validator.validate_chain()
    reader = LedgerReader(ledger_path)
    entries = list(reader.read_all_entries())

    # Replay for cost/token extraction + determinism check.
    # [AMEND-2026-07-22 CRITICAL#3] except amplified to Exception —
    # reconstruct_state() can raise json.JSONDecodeError, KeyError,
    # ValueError in addition to ReplayIntegrityError.
    re = ReplayEngine(ledger_path)
    state = None
    try:
        state = re.reconstruct_state()
        s2 = re.reconstruct_state()
        replay_deterministic = (state == s2)
        state_ok = True
    except Exception:
        state = {}
        state_ok = False
        replay_deterministic = False

    # Components identified by event_type observed
    event_types_seen = sorted({e["event"]["event_type"] for e in entries})
    components = []
    if "LLM_INVOKED" in event_types_seen:
        components.append("llm")
    if "TOOL_CALLED" in event_types_seen:
        components.append("tool")
    if "RETRIEVAL_DONE" in event_types_seen:
        components.append("retrieval")
    if "MEMORY_OP" in event_types_seen:
        components.append("memory")
    if "AGENT_HANDOFF" in event_types_seen:
        components.append("agent")
    if "REASONING_STEP" in event_types_seen:
        components.append("reasoning")

    # Cost & tokens from state
    cost_entries = state.get("cost_accounted", [])
    total_cost_usd = sum(
        c.get("cost", 0)
        for c in cost_entries
        if c.get("currency", "USD") == "USD"
    )
    total_tokens = sum(
        c.get("tokens_in", 0) + c.get("tokens_out", 0)
        for c in cost_entries
    )

    # Incidents: sandbox violations
    incidents_detected = len(state.get("sandbox_violations", []))

    # Sentinel
    sentinel = evaluate_rules(ledger_path)

    return {
        "govern": {
            "policy_enforced": vr.is_valid,
            "audit_trail_complete": vr.is_valid,
        },
        "map": {
            "context_documented": len(entries) > 0,
            "components_identified": components,
        },
        "measure": {
            "total_events": len(entries),
            "total_cost_usd": round(total_cost_usd, 6),
            "total_tokens": total_tokens,
            "replay_determinism_verified": replay_deterministic,
        },
        "manage": {
            "incidents_detected": incidents_detected,
            "sentinel_rules_passing": sentinel.all_rules_pass if state_ok else False,
        },
        "_validation": {
            "failure_type": vr.failure_type,
            "failure_position": vr.position,
        },
    }