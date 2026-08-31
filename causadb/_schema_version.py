import json
import os
import tempfile
from datetime import datetime
from typing import Callable, Dict, List, Tuple

CURRENT_SCHEMA_VERSION = "0.2.0"


def migrate_v0_0_to_v0_1(event_dict: dict) -> dict:
    # Returns a NEW dict; never mutates input (idempotency contract).
    migrated = dict(event_dict)
    if "parent_event_id" not in migrated:
        migrated["parent_event_id"] = None
    if "source_type" not in migrated:
        migrated["source_type"] = "agent"
    # Always set schema_version to 0.1.0 — this migration targets v0.1.0
    migrated["schema_version"] = "0.1.0"
    metadata = migrated.get("metadata")
    if isinstance(metadata, dict) and "priority" not in metadata:
        new_metadata = dict(metadata)
        new_metadata["priority"] = None
        migrated["metadata"] = new_metadata
    return migrated


def migrate_v0_1_to_v0_2(event_dict: dict) -> dict:
    migrated = dict(event_dict)
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    return migrated


# Ordered migration pipeline: from earliest version to latest.
MIGRATION_PIPELINE: List[Dict[str, Callable]] = [
    {"from": "0.0.0", "to": "0.1.0", "fn": migrate_v0_0_to_v0_1},
    {"from": "0.1.0", "to": "0.2.0", "fn": migrate_v0_1_to_v0_2},
]


MIGRATIONS: Dict[Tuple[str, str], Callable] = {
    ("0.0.0", "0.1.0"): migrate_v0_0_to_v0_1,
    ("0.1.0", "0.2.0"): migrate_v0_1_to_v0_2,
}


def _migrate_event(event: dict, from_version: str) -> dict:
    """Apply all necessary migrations to bring event to CURRENT_SCHEMA_VERSION."""
    current = from_version
    result = event
    for step in MIGRATION_PIPELINE:
        if current == step["from"]:
            result = step["fn"](result)
            current = step["to"]
    return result


def migrate_ledger(ledger_path: str, chronicle_path: str) -> int:
    # Exención artículo I (Ledger Monism): esta función escribe directo al
    # ledger.log sin pasar por LedgerWriter. Esto está permitido SOLO porque
    # es el protocolo de migración de schema (artículo VIII exención
    # documentada en CAUSADB_MIGRATION_PLAN.md §5 P.13 — alto #12 resuelto
    # por este protocolo). Es una operación de mantenimiento one-shot sobre
    # el archivo crudo del ledger, no tráfico normal de append.
    total_migrated_count = 0
    entries_out = []

    with open(ledger_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            event = entry.get("event", {})
            schema_version = event.get("schema_version", "0.0.0")
            # Normalize old missing versions
            if schema_version == "" or schema_version is None:
                schema_version = "0.0.0"
            if schema_version != CURRENT_SCHEMA_VERSION:
                entry["event"] = _migrate_event(event, schema_version)
                total_migrated_count += 1
            entries_out.append(entry)

    # Atomic write: temp file in same dir, fsync, os.replace.
    ledger_dir = os.path.dirname(os.path.abspath(ledger_path))
    tmp = tempfile.NamedTemporaryFile(
        dir=ledger_dir, mode="w", delete=False, suffix=".tmp"
    )
    try:
        for entry in entries_out:
            tmp.write(json.dumps(entry, sort_keys=True) + "\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, ledger_path)

    # Append BIT-entry to chronicle (artículo IV — causal auditability).
    timestamp = datetime.utcnow().isoformat() + "Z"
    bit_entry = (
        f"## BIT-MIGRATE {timestamp}\n"
        f"**From:** older\n"
        f"**To:** {CURRENT_SCHEMA_VERSION}\n"
        f"**Events migrated:** {total_migrated_count}\n"
    )
    with open(chronicle_path, "a") as f:
        f.write(bit_entry)
        f.flush()
        os.fsync(f.fileno())

    return total_migrated_count
