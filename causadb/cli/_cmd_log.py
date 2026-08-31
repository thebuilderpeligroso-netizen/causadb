"""`causadb log` subcommand — parse JSON, validate schema, append to ledger."""
import json
from typing import Tuple

from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._schema_validator import validate_event_schema


def cmd_log(args) -> Tuple[int, str]:
    """Append an event to the ledger.

    Pipeline (Fall-Closed):
      1. Parse `args.event_json` as JSON (o modo --decision).
      2. Build a `CanonicalEvent` from the parsed dict.
      3. Validate the event with `validate_event_schema` BEFORE appending.
      4. Append via `LedgerWriter.append`.
    Each failure returns (1, json_error_object); success returns (0, json).
    """
    # 0. Modo --decision (GAP-02): GOVERNANCE_DECISION con bit opcional.
    if getattr(args, "decision", False):
        return _cmd_log_decision(args)

    # 1. Parse JSON
    if not args.event_json:
        return (1, json.dumps({
            "error": "event_json is required (or use --decision mode)",
        }))
    try:
        data = json.loads(args.event_json)
    except json.JSONDecodeError as e:
        return (1, json.dumps({
            "error": "Invalid JSON",
            "error_type": "JSONDecodeError",
            "detail": str(e),
        }))

    # 1.5 Load custom event types from config before validating EventType.
    # The registry must be populated BEFORE EventType(data["event_type"])
    # because _missing_ only resolves dynamically for registered types.
    import os
    from causadb._workspace import WorkspaceManager
    from causadb._event_registry import load_from_config
    config_path = WorkspaceManager.discover(os.getcwd())
    if config_path is not None:
        load_from_config(config_path)

    # 2. Build CanonicalEvent
    try:
        event_type = EventType(data["event_type"])
    except (KeyError, ValueError) as e:
        return (1, json.dumps({
            "error": f"Invalid or missing event_type: {e}",
            "error_type": type(e).__name__,
        }))

    metadata = None
    if data.get("metadata") is not None:
        try:
            metadata = EventMetadata(**data["metadata"])
        except TypeError as e:
            return (1, json.dumps({
                "error": f"Invalid metadata: {e}",
                "error_type": "TypeError",
            }))

    try:
        kwargs = dict(
            event_type=event_type,
            ctx_id=data["ctx_id"],
            source=data["source"],
            source_type=data.get("source_type", "agent"),
            payload=data.get("payload", {}),
            parent_event_id=data.get("parent_event_id"),
            schema_version=data.get("schema_version", "0.1.0"),
            metadata=metadata,
        )
        if "event_id" in data:
            kwargs["event_id"] = data["event_id"]
        if "timestamp" in data:
            kwargs["timestamp"] = data["timestamp"]
        event = CanonicalEvent(**kwargs)
    except (KeyError, ValueError, TypeError) as e:
        return (1, json.dumps({
            "error": str(e),
            "error_type": type(e).__name__,
        }))

    # 3. Validate schema BEFORE appending (Fall-Closed)
    vr = validate_event_schema(event)
    if not vr.is_valid:
        return (1, json.dumps({
            "error": "Schema validation failed",
            "error_type": "ValidationError",
            "description": vr.description,
            "failure_type": vr.failure_type,
        }))

    # 4. Resolve ledger path
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    # 5. Append
    try:
        writer = LedgerWriter(ledger)
        writer.append(event)
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    return (0, json.dumps({
        "event_id": event.event_id,
        "hash": writer.last_hash,
        "timestamp": event.timestamp,
    }, sort_keys=True))


def _cmd_log_decision(args) -> Tuple[int, str]:
    """Modo --decision: escribe un GOVERNANCE_DECISION (GAP-02).

    Payload: {reasoning, impact, decision_type, origin, bit_id?}. Si se pasa
    ``--bit``, el evento se enlaza al BIT en el chronicle index (best-effort:
    el ledger es la autoridad; el link es una proyección derivada).
    """
    reasoning = getattr(args, "reasoning", None)
    impact = getattr(args, "impact", None)
    decision_type = getattr(args, "decision_type", None)
    origin = getattr(args, "origin", None)
    bit = getattr(args, "bit", None)

    missing = [name for name, val in (
        ("--reasoning", reasoning), ("--impact", impact),
        ("--decision-type", decision_type), ("--origin", origin),
    ) if not val]
    if missing:
        return (1, json.dumps({
            "error": f"Missing required flags for --decision: {', '.join(missing)}",
        }))

    payload = {
        "reasoning": reasoning,
        "impact": impact,
        "decision_type": decision_type,
        "origin": origin,
    }
    if bit:
        payload["bit_id"] = bit

    try:
        event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="cli",
            source="causadb:cli",
            payload=payload,
        )
    except (KeyError, ValueError, TypeError) as e:
        return (1, json.dumps({
            "error": str(e),
            "error_type": type(e).__name__,
        }))

    vr = validate_event_schema(event)
    if not vr.is_valid:
        return (1, json.dumps({
            "error": "Schema validation failed",
            "error_type": "ValidationError",
            "description": vr.description,
            "failure_type": vr.failure_type,
        }))

    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))

    try:
        writer = LedgerWriter(ledger)
        writer.append(event)
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    # Link best-effort al BIT (GAP-02): el ledger es la autoridad; si el
    # link falla (índice corrupto, etc.) el evento YA está escrito — se
    # reporta linked=False sin fallar el comando.
    linked = False
    if bit:
        try:
            from causadb._chronicle_index import link_events
            link_events(ledger, bit, [event.event_id])
            linked = True
        except Exception:
            linked = False

    return (0, json.dumps({
        "event_id": event.event_id,
        "hash": writer.last_hash,
        "timestamp": event.timestamp,
        "bit_id": bit,
        "linked": linked,
    }, sort_keys=True))
