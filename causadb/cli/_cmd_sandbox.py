import json
from causadb._replay_engine import ReplayEngine


def cmd_sandbox(args) -> tuple:
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        engine = ReplayEngine(ledger)
        state = engine.reconstruct_state()
        return (0, json.dumps({
            "violations": state["sandbox_violations"],
            "total_mutations": len(state["sandbox_mutations"]) + len(state["sandbox_violations"])
        }, default=str, sort_keys=True))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))