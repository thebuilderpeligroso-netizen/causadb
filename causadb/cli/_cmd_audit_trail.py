"""CLI handler for `causadb audit-trail` (F.7.4).

Exports the audit trail in JSON or text format. Lives in CLI (not in
`causadb/compliance/`) because the "audit trail" is the raw representation
of the ledger, not an interpreted report (Article VIII — no abstraction
with 1 implementation).

Pattern A: returns `(exit_code, output_str)`. If `--output` is given,
writes file + returns metadata JSON. Otherwise returns the content string.
"""

import json
import os
from datetime import datetime
from typing import Tuple

from causadb._ledger_reader import LedgerReader
from causadb._ledger_validator import LedgerValidator


def _format_json(entries, vr, ledger_path: str) -> str:
    """Build a JSON audit trail representation."""
    # Time range + event types count
    timestamps = []
    event_types_count = {}
    for e in entries:
        ev = e["event"]
        ts = ev["timestamp"]
        timestamps.append(ts)
        et = ev["event_type"]
        event_types_count[et] = event_types_count.get(et, 0) + 1

    summary = {
        "total_events": len(entries),
        "total_lines": len(entries),
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
        "event_types_count": event_types_count,
    }

    validation = {
        "is_valid": vr.is_valid,
        "failure_type": vr.failure_type,
        "failure_position": vr.position,
    }

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "ledger_path": ledger_path,
        "validation": validation,
        "summary": summary,
        "entries": entries,
    }

    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _format_text(entries, vr, ledger_path: str) -> str:
    """Build a human-readable text audit trail representation."""
    lines = []
    lines.append("=== CausaDB Audit Trail ===")
    lines.append(f"Generated: {datetime.utcnow().isoformat() + 'Z'}")
    lines.append(f"Ledger: {ledger_path}")

    if vr.is_valid:
        lines.append("Hash-chain validation: PASS")
    else:
        lines.append(f"Hash-chain validation: FAIL ({vr.failure_type} at {vr.position})")
    lines.append("")

    # Summary block
    timestamps = []
    event_types_count = {}
    for e in entries:
        ev = e["event"]
        timestamps.append(ev["timestamp"])
        et = ev["event_type"]
        event_types_count[et] = event_types_count.get(et, 0) + 1

    lines.append("Summary:")
    lines.append(f"- Total events: {len(entries)}")
    if timestamps:
        lines.append(f"- Time range: {timestamps[0]} -> {timestamps[-1]}")
    types_str = ", ".join(f"{k} ({v})" for k, v in sorted(event_types_count.items()))
    if types_str:
        lines.append(f"- Event types: {types_str}")
    else:
        lines.append("- Event types: (none)")
    lines.append("")

    # Entries block
    lines.append("Entries (chronological):")
    for i, e in enumerate(entries, start=1):
        ev = e["event"]
        etype = ev["event_type"]
        ts = ev["timestamp"]
        eid = ev["event_id"]
        payload = ev.get("payload", {}) or {}
        payload_str = ", ".join(f"{k}={v}" for k, v in payload.items())
        h = e.get("hash", "")[:16] if e.get("hash") else ""
        lines.append("")
        lines.append(f"[#{i}] {ts}  {etype}")
        lines.append(f"     event_id:  {eid}")
        if payload_str:
            lines.append(f"     payload:   {payload_str}")
        if h:
            lines.append(f"     hash:      {h}")

    return "\n".join(lines)


def cmd_audit_trail(args) -> Tuple[int, str]:
    fmt = args.format
    output_path = args.output

    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        if not os.path.exists(ledger):
            return (
                1,
                json.dumps({"error": f"ledger not found: {ledger}"}),
            )
        reader = LedgerReader(ledger)
        entries = list(reader.read_all_entries())
        validator = LedgerValidator(ledger)
        vr = validator.validate_chain()

        if fmt == "json":
            content = _format_json(entries, vr, ledger)
        elif fmt == "text":
            content = _format_text(entries, vr, ledger)
        else:
            return (1, json.dumps({"error": f"unknown format: {fmt}"}))

        if output_path:
            with open(output_path, "w") as f:
                f.write(content)
            return (
                0,
                json.dumps({
                    "written_to": output_path,
                    "format": fmt,
                    "bytes": len(content),
                }, sort_keys=True),
            )
        else:
            return (0, content)
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )