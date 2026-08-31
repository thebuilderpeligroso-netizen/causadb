"""causadb._explain — Explain governance decisions and their causal lineage.

This module provides functionality to explain why a particular governance
decision was made by tracing its causal lineage in the ledger.

Article II: thin wrapper over core logic, no reimplementation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from causadb._ledger_reader import LedgerReader
from causadb._event_types import EventType


def _get_event_by_id(ledger_path: str, event_id: str) -> Optional[dict]:
    """Retrieve an event by its ID from the ledger."""
    reader = LedgerReader(ledger_path)
    for event in reader.read_all():
        if event.event_id == event_id:
            return {
                "event_id": event.event_id,
                "event_type": event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type),
                "timestamp": event.timestamp,
                "source": event.source,
            "parent_event_id": event.parent_event_id,
                "payload": event.payload,
            }
    return None


def _walk_back_from_event(ledger_path: str, start_event_id: str) -> List[dict]:
    """Walk back from a given event via parent_event_id to build the causal chain.

    Returns a list of events from the start event back to the root (oldest first).
    """
    reader = LedgerReader(ledger_path)
    all_events = list(reader.read_all())
    by_id = {e.event_id: e for e in all_events}

    chain = []
    current_id = start_event_id
    seen = set()
    while current_id is not None and current_id not in seen:
        event = by_id.get(current_id)
        if event is None:
            break
        event_dict = {
            "event_id": event.event_id,
            "event_type": event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type),
            "timestamp": event.timestamp,
            "source": event.source,
            "parent_event_id": event.parent_event_id,
            "payload": event.payload,
        }
        chain.append(event_dict)
        seen.add(current_id)
        current_id = event.parent_event_id
        if current_id is None or current_id == "GENESIS":
            break
    # Reverse to get root first, then the decision event last
    chain.reverse()
    return chain


def explain_decision(ledger_path: str, event_id: str) -> dict:
    """Explain a governance decision by tracing its causal lineage.

    Args:
        ledger_path: Path to the ledger file.
        event_id: The ID of the decision event to explain.

    Returns:
        A dictionary with the decision event and its causal chain.

    Raises:
        ValueError: If the event is not found or is not a governance decision.
    """
    # Fetch the event
    event = _get_event_by_id(ledger_path, event_id)
    if event is None:
        raise ValueError(f"Event '{event_id}' not found in ledger '{ledger_path}'.")

    # Check if it's a governance decision event
    # We assume the event type is "GOVERNANCE_DECISION" as per causadb_log_decision
    if event["event_type"] != "GOVERNANCE_DECISION":
        raise ValueError(
            f"Event '{event_id}' is of type '{event['event_type']}', "
            f"expected 'GOVERNANCE_DECISION' for a decision explanation."
        )

    # Build the causal chain from this event back to the root
    chain = _walk_back_from_event(ledger_path, event_id)

    # The chain now includes the decision event at the end (since we reversed)
    # We can separate the decision event from its ancestors if desired.
    # For simplicity, we return the whole chain and mark the decision event.
    # The decision event is the last one in the chain (since we went from root to decision).
    decision_event = chain[-1] if chain else None
    ancestral_chain = chain[:-1]  # Everything before the decision event

    # Build a human-readable explanation (optional, but we can include a summary)
    explanation_parts = [
        f"Decision event {event_id} of type {event['event_type']} at {event['timestamp']}.",
        f"Reasoning: {event['payload'].get('reasoning', 'N/A')}",
        f"Impact: {event['payload'].get('impact', 'N/A')}",
    ]
    if ancestral_chain:
        explanation_parts.append(
            f"Causal chain has {len(ancestral_chain)} predecessor event(s)."
        )
    else:
        explanation_parts.append("No parent events found; this is a root decision.")
    explanation = " ".join(explanation_parts)

    return {
        "decision_event": decision_event,
        "ancestral_chain": ancestral_chain,
        "explanation": explanation,
        "full_chain": chain,  # Includes the decision event at the end
    }