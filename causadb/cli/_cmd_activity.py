"""`causadb activity` subcommand — thin wrapper (Art. II) around the
Agent Activity Report (H8.5). Delegates all logic to
``causadb._agent_activity_report``. Pattern A: returns
``(exit_code, output_str)``; ``main.py`` is the single place that prints.
"""

import json
from typing import Tuple

from causadb._agent_activity_report import build_agent_activity_report
from causadb._ledger_reader import LedgerReader
from causadb._replay_engine import ReplayEngine
from causadb._workspace import resolve_ledger


def cmd_activity(args) -> Tuple[int, str]:
    """Consolidar qué hizo un agente desde la proyección del ReplayEngine.

    El reporte consume SOLO la proyección ya computada (Art. V). El ledger
    se lee únicamente para proveer ``events`` a ``cost_consistency``
    (``CostRollup.validate_hermes_consistency``).
    """
    try:
        ledger = resolve_ledger(args.ledger)
        state = ReplayEngine(ledger).reconstruct_state()
        events = list(LedgerReader(ledger).read_all_entries())
        result = build_agent_activity_report(
            state,
            session_id=getattr(args, "session", None),
            from_time=getattr(args, "from_time", None),
            to_time=getattr(args, "to_time", None),
            events=events,
        )
        return (0, json.dumps(result, sort_keys=True, default=str))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
