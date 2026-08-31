
import json
import argparse
from typing import Tuple
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._ledger_reader import LedgerReader
from types import MappingProxyType

def cmd_snapshot(args) -> Tuple[int, str]:
    from causadb._workspace import resolve_ledger

    try:
        ledger_path = resolve_ledger(args.ledger)
    except Exception as e:
        return (1, json.dumps({"error": f"Workspace error: {e}"}))

    try:
        # Contar eventos
        reader = LedgerReader(ledger_path)
        all_entries = list(reader.read_all_entries())
        total_events = len(all_entries)

        payload = {
            "total_events": total_events,
            "total_tests": args.tests,
            "fases_completadas": args.fases.split(",") if args.fases else [],
            "bloqueantes_resueltos": args.bloqueantes,
            "notas": args.notas or ""
        }

        event = CanonicalEvent(
            event_type=EventType.PROJECT_SNAPSHOT,
            ctx_id="snapshot",
            source="causadb:cli",
            payload=MappingProxyType(payload),
        )

        writer = LedgerWriter(ledger_path)
        written = writer.append(event)
        
        event_id = written["event"]["event_id"]
        
        # Link to chronicle if requested
        if getattr(args, "chronicle_ref", None):
            from causadb._chronicle_index import link_events
            link_events(ledger_path, args.chronicle_ref, [event_id])

        return (0, json.dumps({
            "status": "success",
            "event_id": event_id,
            "hash": written["hash"]
        }, indent=2))

    except Exception as e:
        return (1, json.dumps({"error": f"Snapshot failed: {e}"}))

