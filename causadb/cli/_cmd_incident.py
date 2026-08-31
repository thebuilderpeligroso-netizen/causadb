"""CLI handler for `causadb incident` (F.7.3).

Pattern A: returns `(exit_code, output_str)` where `output_str` is JSON.
"""

import json


def cmd_incident(args) -> tuple:
    ledger = args.ledger
    event_id = args.event_id

    if not event_id:
        return (1, json.dumps({"error": "event-id required"}))

    try:
        from causadb.compliance._incident_response import generate_incident_report
        report = generate_incident_report(ledger, event_id)
        return (0, json.dumps(report, default=str, sort_keys=True))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )