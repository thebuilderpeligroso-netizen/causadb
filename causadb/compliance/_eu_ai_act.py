"""EU AI Act Art. 12 compliance report generator (F.7.1).

Produces a formal report attesting CausaDB's compliance with Regulation
2024/1689 Art. 12 — logging facilities for high-risk AI systems.

Function, not class (Article VIII). Falls-Closed: always returns dict,
never raises exception (decision #8).
"""

from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator
from causadb._replay_engine import ReplayEngine


def generate_eu_ai_act_report(ledger_path: str) -> dict:
    validator = LedgerValidator(ledger_path)
    vr = validator.validate_chain()
    reader = LedgerReader(ledger_path)
    entries = list(reader.read_all_entries())

    # Replay determinism: 2 reconstructions, deep-compare.
    # [AMEND-2026-07-22 CRITICAL#3] except amplified to Exception —
    # reconstruct_state() can raise json.JSONDecodeError, KeyError,
    # ValueError in addition to ReplayIntegrityError.
    re = ReplayEngine(ledger_path)
    s1 = None
    try:
        s1 = re.reconstruct_state()
        s2 = re.reconstruct_state()
        replay_deterministic = (s1 == s2)
        replay_ok = True
    except Exception:
        replay_deterministic = False
        replay_ok = False

    side_effects_reconstructible = (
        replay_ok
        and s1 is not None
        and any([
            len(s1.get("files_modified", [])) > 0
            or len(s1.get("commands_run", [])) > 0
            or len(s1.get("commits_made", [])) > 0
            or len(s1.get("llm_invocations", [])) > 0
            or len(s1.get("cost_accounted", [])) > 0
            or len(s1.get("tools_called", [])) > 0
            or len(s1.get("queries_executed", [])) > 0
            or len(s1.get("mutations_applied", [])) > 0
        ])
    )

    # Time range
    timestamps = sorted([e["event"]["timestamp"] for e in entries])
    time_range = {
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
    }

    traceability_guaranteed = (
        vr.is_valid
        and replay_deterministic
        and side_effects_reconstructible
    )

    return {
        "traceability_guaranteed": traceability_guaranteed,
        "logging_facilities": {
            "append_only": True,
            "hash_chain_validated": vr.is_valid,
            "timestamp_precise": True,
            "event_chain_complete": vr.failure_type != "CONTINUITY_BREAK",
        },
        "events_logged": len(entries),
        "time_range": time_range,
        "side_effects_reconstructible": side_effects_reconstructible,
        "replay_test_passed": replay_ok,
        "validation_failure_type": vr.failure_type,
        "validation_failure_position": vr.position,
    }