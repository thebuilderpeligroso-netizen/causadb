"""AI Incident Response report generator (F.7.3).

Generates an AIID-compatible incident report given a ledger path and an
incident event_id. Uses causal chain traversal via parent_event_id.

Function, not class (Article VIII). Falls-Closed: always returns dict.
"""

from typing import List, Dict, Any

from causadb._ledger_reader import LedgerReader
from causadb._sentinel_rules import evaluate_rules


def _trace_causal_chain(ledger_path: str, incident_event_id: str) -> tuple:
    """Trace causal chain from incident_event_id toward root via parent_event_id.

    Returns: (chain, truncated) where:
      - chain is a list of entries ordered root -> incident.
      - truncated is True if loop detection broke the traversal (visited set).
    Returns ([], False) if event_id not in ledger.
    """
    reader = LedgerReader(ledger_path)
    by_id = {e["event"]["event_id"]: e for e in reader.read_all_entries()}

    if incident_event_id not in by_id:
        return ([], False)

    chain = []
    visited = set()
    truncated = False
    current = incident_event_id
    while current and current in by_id:
        if current in visited:
            truncated = True
            break
        visited.add(current)
        entry = by_id[current]
        chain.append(entry)
        current = entry["event"].get("parent_event_id")

    chain.reverse()
    return (chain, truncated)


_RECOMMENDED_RULES_BY_TYPE = {
    "FILE_MODIFIED": ["block_writes_outside_workspace", "require_sandbox_for_file_ops"],
    "SANDBOX_STATE": ["block_writes_outside_workspace"],
    "LLM_INVOKED": ["require_prompt_validation"],
    "TOOL_CALLED": ["require_tool_safety_check"],
}


def generate_incident_report(ledger_path: str, incident_event_id: str) -> dict:
    reader = LedgerReader(ledger_path)
    by_id = {e["event"]["event_id"]: e for e in reader.read_all_entries()}

    chain_entries, chain_truncated = _trace_causal_chain(ledger_path, incident_event_id)

    if not chain_entries:
        # event_id not in ledger
        return {
            "what_happened": "unknown",
            "why_it_happened": {
                "triggering_event": "",
                "causal_chain": [],
                "root_cause_event_id": None,
                "chain_length": 0,
                "chain_truncated": False,
            },
            "remediation": {
                "action_taken": "",
                "revert_possible": False,
                "revert_target_event_id": None,
            },
            "prevention": {
                "sentinel_rules_triggered": [],
                "recommended_rules": [],
            },
            "_estimated": True,
        }

    incident_entry = chain_entries[-1]
    incident_event = incident_entry["event"]
    incident_type = incident_event["event_type"]
    incident_ts = incident_event["timestamp"]
    incident_payload = incident_event.get("payload", {}) or {}

    root_cause_entry = chain_entries[0]
    root_cause_event_id = root_cause_entry["event"]["event_id"]

    # what_happened
    what = f"{incident_type} at {incident_ts} (event_id={incident_event_id[:8]})"
    if incident_type == "FILE_MODIFIED":
        path = incident_payload.get("path", "unknown")
        action = incident_payload.get("action", "unknown")
        what = (
            f"{incident_type} at {incident_ts} "
            f"(event_id={incident_event_id[:8]}): "
            f"Agent modified {path} (action={action})"
        )

    # triggering_event
    if len(chain_entries) >= 2:
        trigger = chain_entries[1]["event"]
        triggering_event = f"{trigger['event_type']} (event_id={trigger['event_id'][:8]})"
    else:
        triggering_event = f"{incident_type} (event_id={incident_event_id[:8]})"

    # Compress chain entries for the report (event_id, event_type, timestamp)
    causal_chain = [
        {
            "event_id": e["event"]["event_id"],
            "event_type": e["event"]["event_type"],
            "timestamp": e["event"]["timestamp"],
        }
        for e in chain_entries
    ]

    # remediation: scan for MUTATION_REVERTED with revert_target_event_id == incident
    revert_possible = False
    revert_target_event_id = None
    action_taken = ""
    for e in reader.read_all_entries():
        ev = e["event"]
        if ev["event_type"] == "MUTATION_REVERTED":
            target = ev.get("payload", {}).get("revert_target_event_id")
            if target == incident_event_id:
                revert_possible = True
                revert_target_event_id = target
                action_taken = "MUTATION_REVERTED detected"
                break
    if not action_taken:
        action_taken = incident_type

    # prevention: sentinel rules
    try:
        sentinel = evaluate_rules(ledger_path)
        triggered = [r.rule_name for r in sentinel.results if not r.passed]
    except Exception:
        triggered = []

    recommended = _RECOMMENDED_RULES_BY_TYPE.get(incident_type, [])

    return {
        "what_happened": what,
        "why_it_happened": {
            "triggering_event": triggering_event,
            "causal_chain": causal_chain,
            "root_cause_event_id": root_cause_event_id,
            "chain_length": len(causal_chain),
            "chain_truncated": chain_truncated,
        },
        "remediation": {
            "action_taken": action_taken,
            "revert_possible": revert_possible,
            "revert_target_event_id": revert_target_event_id,
        },
        "prevention": {
            "sentinel_rules_triggered": triggered,
            "recommended_rules": recommended,
        },
        "_estimated": False,
    }