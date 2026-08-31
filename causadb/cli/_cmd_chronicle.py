import json
from typing import Tuple

def cmd_chronicle(args) -> Tuple[int, str]:
    """Route ``causadb chronicle list|events|ref|link|rebuild-index|append|append-md|migrate|reconstruct``."""
    from causadb._workspace import resolve_ledger, NoWorkspaceError
    from causadb._chronicle_index import (
        list_entries, query_by_bit, query_by_event, link_events, rebuild_index
    )

    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))

    if args.action == "list":
        entries = list_entries(ledger)
        if getattr(args, "unlinked", False):
            entries = [e for e in entries if e.get("event_count", 0) == 0]
        return (0, json.dumps(entries, indent=2))
    elif args.action == "events":
        if not args.bit:
            return (1, json.dumps({"error": "--bit is required for 'events' action"}))
        eids = query_by_bit(ledger, args.bit)
        return (0, json.dumps(eids, indent=2))
    elif args.action == "ref":
        if not args.event_id:
            return (1, json.dumps({"error": "event_id is required for 'ref' action"}))
        bits = query_by_event(ledger, args.event_id)
        return (0, json.dumps(bits, indent=2))
    elif args.action == "link":
        if not args.bit or not args.event_ids:
            return (1, json.dumps({"error": "--bit and --event-ids are required for 'link' action"}))
        result = link_events(ledger, args.bit, args.event_ids.split(","))
        return (0, json.dumps(result, indent=2))
    elif args.action == "rebuild-index":
        chronicle_path = getattr(args, "chronicle_path", None)
        try:
            result = rebuild_index(ledger, chronicle_path=chronicle_path)
        except FileNotFoundError as e:
            return (1, json.dumps({"error": str(e)}))
        return (0, json.dumps({"status": "ok", "bits_found": len(result["by_bit"])}, indent=2))
    elif args.action == "append":
        return _cmd_chronicle_append(args, ledger)
    elif args.action == "append-md":
        return _cmd_chronicle_append_md(args, ledger)
    elif args.action == "migrate":
        return _cmd_chronicle_migrate(args, ledger)
    elif args.action == "reconstruct":
        return _cmd_chronicle_reconstruct(args, ledger)
    else:
        return (1, json.dumps({"error": f"Unknown action: {args.action}"}))


def _cmd_chronicle_append(args, ledger: str) -> Tuple[int, str]:
    """Append a CHRONICLE_ENTRY to the ledger via LedgerWriter."""
    from causadb._event_types import EventType
    from causadb._event_schema import CanonicalEvent
    from causadb._ledger_writer import LedgerWriter
    from causadb._schema_validator import validate_event_schema

    # Validate required fields
    bit = getattr(args, "bit", None)
    title = getattr(args, "title", None)
    date = getattr(args, "date", None)
    maker = getattr(args, "maker", None)
    checker = getattr(args, "checker", None)
    summary = getattr(args, "summary", None)
    files = getattr(args, "files", None) or []

    if not all([bit, title, date, maker, checker, summary]):
        return (1, json.dumps({
            "error": "Missing required fields. Required: --bit, --title, --date, --maker, --checker, --summary"
        }))

    payload = {
        "bit_id": bit,
        "title": title,
        "date": date,
        "maker": maker,
        "checker": checker,
        "summary": summary,
        "files_touched": list(files) if files else [],
    }

    event = CanonicalEvent(
        event_type=EventType.CHRONICLE_ENTRY,
        ctx_id="chronicle",
        source="causadb:chronicle",
        payload=payload,
    )

    # Validate schema before writing (Artículo I — Fall-Closed)
    result = validate_event_schema(event)
    if not result.is_valid:
        return (1, json.dumps({
            "error": f"Schema validation failed: {result.description}",
            "failure_type": result.failure_type,
        }))

    # Write via LedgerWriter (Artículo I — never open(ledger.log, "a"))
    writer = LedgerWriter(ledger)
    entry = writer.append(event)

    # Auto-link best-effort (GAP-02): el CHRONICLE_ENTRY lleva bit_id en el
    # payload (autoridad del ledger); el link en el índice es una proyección
    # derivada — si falla, el evento ya está escrito (linked=False).
    linked = False
    try:
        from causadb._chronicle_index import link_events
        link_events(ledger, bit, [event.event_id])
        linked = True
    except Exception:
        linked = False

    return (0, json.dumps({
        "event_id": event.event_id,
        "hash": entry["hash"],
        "bit_id": bit,
        "status": "appended",
        "linked": linked,
    }))


def _cmd_chronicle_append_md(args, ledger: str) -> Tuple[int, str]:
    """Append a BIT-entry to CAUSADB_CHRONICLE.md (template curado).

    Sedimentación narrativa al .md (Layer 3, humana) — NO toca el ledger.
    Idempotente: bit_id duplicado → exit≠0 ``{"status": "already_exists"}``.
    FAIL-CLOSED: chronicle no resuelto o campos faltantes → exit≠0.
    """
    from causadb._chronicle_append import append_entry

    chronicle_path = getattr(args, "chronicle_path", None)
    bit = getattr(args, "bit", None)
    title = getattr(args, "title", None)
    date = getattr(args, "date", None)
    author = getattr(args, "author", None)
    nature = getattr(args, "nature", None)
    summary = getattr(args, "summary", None)
    files = getattr(args, "files", None) or []
    body = getattr(args, "body", None)
    event_id = getattr(args, "event_id", None)

    if not all([bit, title, date, author, body]):
        return (1, json.dumps({
            "error": "Missing required fields. Required: --bit, --title, --date, --author, --body"
        }))

    try:
        result = append_entry(
            ledger,
            chronicle_path=chronicle_path,
            bit_id=bit,
            title=title,
            date=date,
            author=author,
            nature=nature,
            summary=summary,
            files=files,
            body=body,
            event_id=event_id,
        )
    except ValueError as e:
        return (1, json.dumps({"error": str(e)}))

    if result.get("status") == "already_exists":
        return (1, json.dumps(result))
    return (0, json.dumps(result))


def _cmd_chronicle_reconstruct(args, ledger: str) -> Tuple[int, str]:
    """Replay parcial hasta la frontera del BIT (GAP-02; plan §4.2).

    Frontera = el event_id enlazado con mayor sequence_number de append
    (D1: append-order, no time-order — un evento backdated apendeado después
    NO se incluye). ``--time`` override → replay por timestamp.

    FAIL-CLOSED:
      - BIT sin eventos enlazados → error (exit≠0).
      - event_id enlazado que NO existe en el ledger (ghost) → error
        (exit≠0): el índice referencia un evento fantasma.
    """
    from causadb._chronicle_index import query_by_bit
    from causadb._ledger_index import LedgerIndex
    from causadb._replay_engine import ReplayEngine

    bit = getattr(args, "bit", None)
    if not bit:
        return (1, json.dumps({"error": "--bit is required for 'reconstruct' action"}))
    eids = query_by_bit(ledger, bit)
    if not eids:
        return (1, json.dumps({"error": f"No events linked to BIT {bit}"}))

    # Validar ghost + frontera por seq de append (no pos, no timestamp).
    index = LedgerIndex(ledger)
    frontier_eid = None
    frontier_seq = -1
    for eid in eids:
        offset = index.get_offset(eid)
        if offset is None:
            return (1, json.dumps({
                "error": f"event_id {eid} linked to BIT {bit} does not exist "
                         "in the ledger (ghost); rebuild the index",
            }))
        seq = index.event_ids[eid][1]
        if seq > frontier_seq:
            frontier_seq = seq
            frontier_eid = eid

    to_time = getattr(args, "time", None)
    try:
        state = ReplayEngine(ledger).reconstruct_state(
            to_time=to_time, until_event_id=frontier_eid
        )
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    return (0, json.dumps({
        "bit": bit,
        "frontier_event_id": frontier_eid,
        "events_applied": state.get("events_applied", 0),
        "files_modified": state.get("files_modified", []),
        "last_hash": state.get("last_hash"),
        "timestamp": state.get("timestamp"),
    }, indent=2))


def _cmd_chronicle_migrate(args, ledger: str) -> Tuple[int, str]:
    """Migrate entries from CAUSADB_CHRONICLE.md into the ledger as CHRONICLE_ENTRY events."""
    from causadb._chronicle_migrate import parse_chronicle_md
    from causadb._event_types import EventType
    from causadb._event_schema import CanonicalEvent
    from causadb._ledger_writer import LedgerWriter
    from causadb._schema_validator import validate_event_schema

    chronicle_path = getattr(args, "chronicle_path", None)
    if not chronicle_path:
        return (1, json.dumps({"error": "--chronicle-path is required for 'migrate' action"}))

    entries = parse_chronicle_md(chronicle_path)
    if not entries:
        return (1, json.dumps({
            "error": f"No BIT entries found in chronicle: {chronicle_path}"
        }))

    writer = LedgerWriter(ledger)
    migrated = 0
    errors = []

    for entry_dict in entries:
        payload = {
            "bit_id": entry_dict["bit_id"],
            "title": entry_dict["title"],
            "date": entry_dict["date"],
            "maker": entry_dict["maker"],
            "checker": entry_dict["checker"],
            "summary": entry_dict["summary"],
            "files_touched": entry_dict.get("files_touched", []),
        }

        event = CanonicalEvent(
            event_type=EventType.CHRONICLE_ENTRY,
            ctx_id="chronicle",
            source="causadb:chronicle",
            payload=payload,
        )

        validation = validate_event_schema(event)
        if not validation.is_valid:
            errors.append({
                "bit_id": entry_dict["bit_id"],
                "error": validation.description,
            })
            continue

        try:
            writer.append(event)
            migrated += 1
        except Exception as e:
            errors.append({
                "bit_id": entry_dict["bit_id"],
                "error": str(e),
            })

    result = {
        "status": "ok" if not errors else "partial",
        "entries_migrated": migrated,
        "entries_found": len(entries),
    }
    if errors:
        result["errors"] = errors

    return (0 if not errors else 1, json.dumps(result, indent=2))