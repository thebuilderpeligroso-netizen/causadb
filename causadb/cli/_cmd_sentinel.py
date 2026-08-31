"""`causadb sentinel` subcommand — thin wrapper around `evaluate_rules`."""
import json
from typing import Tuple

from causadb._sentinel_rules import evaluate_rules


def cmd_sentinel(args) -> Tuple[int, str]:
    """Delegate to `evaluate_rules(ledger)` and serialize the SentinelReport."""
    try:
        rep = evaluate_rules(args.ledger)
        return (0, json.dumps({
            "all_rules_pass": rep.all_rules_pass,
            "summary": rep.summary,
            "results": [
                {"rule_name": r.rule_name, "passed": r.passed}
                for r in rep.results
            ],
        }, sort_keys=True))
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
