"""CLI handler for `causadb compliance` (F.7.1 + F.7.2).

Pattern A: returns `(exit_code, output_str)` where `output_str` is JSON.
`main.py` is the single place that calls `print()`.
"""

import json


def cmd_compliance(args) -> tuple:
    framework = args.framework
    ledger = args.ledger

    try:
        if framework == "eu-ai-act":
            from causadb.compliance._eu_ai_act import generate_eu_ai_act_report
            report = generate_eu_ai_act_report(ledger)
            return (0, json.dumps(report, default=str, sort_keys=True))
        elif framework == "nist-ai-rmf":
            from causadb.compliance._nist_ai_rmf import generate_nist_report
            report = generate_nist_report(ledger)
            return (0, json.dumps(report, default=str, sort_keys=True))
        else:
            return (
                1,
                json.dumps({"error": f"unknown framework: {framework}"}),
            )
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )