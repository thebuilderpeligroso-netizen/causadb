"""`causadb genesis import` — motor Génesis (onboarding one-shot).

Para proyectos YA comenzados, sin daemon previo corriendo. Ingiere el
historial de fuentes (git, filesystem, obsidian) en un ledger nuevo con
``provenance="genesis"`` en el payload de cada evento, y emite un
CODEBASE_ARCHITECTURE_SNAPSHOT + GENESIS_SUMMARY.

Aislamiento de cursores: se instancia un ``Harvester`` DEDICADO con
``config_path="<ledger_dir>/.genesis_cursors.json"`` para NO contaminar los
cursores del daemon live (``.harvester_cursors.json``).

Degradación suave: si no hay fuentes detectadas, no bloquea (exit 0 con
mensaje). No implementa handoff de cursor (decisión del operador — complejidad
innecesaria para un one-shot).
"""

import datetime
import json
import os
import uuid
from types import MappingProxyType
from typing import Optional

from causadb._harvester import Harvester
from causadb._workspace import resolve_ledger, NoWorkspaceError

# Fuentes de génesis (para --all).
GENESIS_SOURCES = ("git", "filesystem", "obsidian")


class GenesisHarvester(Harvester):
    """Harvester dedicado que inyecta ``provenance="genesis"`` en el payload
    de cada evento importado (aditivo, sin tocar el schema)."""

    def _event_from_raw(self, source_type: str, raw: dict):
        event = super()._event_from_raw(source_type, raw)
        payload = dict(event.payload)
        payload["provenance"] = "genesis"
        object.__setattr__(event, "payload", MappingProxyType(payload))
        return event


def _resolve_project_id(ledger_path: str) -> str:
    """project_id desde el config del workspace, o genera uno nuevo."""
    try:
        from causadb._workspace import WorkspaceManager
        config_path = os.path.join(os.path.dirname(ledger_path), "config.json")
        if os.path.isfile(config_path):
            ws = WorkspaceManager.load(config_path)
            if getattr(ws, "project_id", None):
                return ws.project_id
    except Exception:
        pass
    return str(uuid.uuid4())


def _write_codebase_snapshot_event(ledger_path: str, snapshot: dict) -> None:
    """Emite el CODEBASE_ARCHITECTURE_SNAPSHOT con el payload en BlobStore.

    El snapshot completo (nodes/edges) va al blob; el payload lleva la
    metadata + la referencia ``$blob``. LedgerReader lo resuelve al leer.
    """
    from causadb._blob_store import BlobStore
    from causadb._config import CausaDBConfig
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._ledger_writer import LedgerWriter

    config = CausaDBConfig(ledger_path=ledger_path)
    store = BlobStore(config.blob_store_path)
    blob_hash = store.put(snapshot)
    payload = {
        "project_id": snapshot["project_id"],
        "generated_at": snapshot["generated_at"],
        "generator": snapshot["generator"],
        "$blob": blob_hash,
    }
    writer = LedgerWriter(ledger_path, config=config)
    ev = CanonicalEvent(
        event_type=EventType("CODEBASE_ARCHITECTURE_SNAPSHOT"),
        ctx_id="genesis",
        source="causadb:genesis",
        payload=MappingProxyType(payload),
    )
    writer.append(ev)


def _write_genesis_summary(ledger_path: str, project_id: str, counts: dict) -> dict:
    """Emite el GENESIS_SUMMARY con el resumen de la ingesta."""
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._ledger_writer import LedgerWriter

    total = sum(counts.values())
    summary = {
        "project_id": project_id,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": counts,
        "events_imported": total,
        "summary": f"Genesis import completado: {total} eventos de {len(counts)} fuente(s)",
    }
    writer = LedgerWriter(ledger_path)
    ev = CanonicalEvent(
        event_type=EventType("GENESIS_SUMMARY"),
        ctx_id="genesis",
        source="causadb:genesis",
        payload=MappingProxyType(summary),
    )
    writer.append(ev)
    return summary


def run_genesis_import(
    ledger_path: str,
    source: str = "--all",
    source_path: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Ejecuta la ingesta one-shot de génesis.

    Args:
        ledger_path: Ruta absoluta al ledger.
        source: ``--all`` (git+filesystem+obsidian) o una fuente específica.
        source_path: Directorio del proyecto/fuente (default: CWD).
        project_id: Override del project_id (default: del config o nuevo).

    Returns:
        dict con ``project_id``, ``sources`` (counts), ``events_imported``,
        ``codebase_generator`` y ``summary``.
    """
    ledger_path = os.path.abspath(ledger_path)
    project_id = project_id or _resolve_project_id(ledger_path)
    source_path = os.path.abspath(source_path) if source_path else os.getcwd()

    # Harvester DEDICADO con cursores aislados del daemon live.
    genesis_harvester = GenesisHarvester(
        ledger_path,
        config_path=os.path.join(os.path.dirname(ledger_path), ".genesis_cursors.json"),
    )

    if source in ("--all", "git"):
        from causadb._harvest_source_git import GitReflogSource
        genesis_harvester.register_source(
            GitReflogSource(source_path=source_path, ledger_path=ledger_path)
        )
    if source in ("--all", "filesystem"):
        from causadb._harvest_source_filesystem import FilesystemSource
        genesis_harvester.register_source(
            FilesystemSource(ledger_path=ledger_path, project_root=source_path)
        )
    if source in ("--all", "obsidian"):
        from causadb._harvest_source_obsidian import ObsidianSource
        genesis_harvester.register_source(
            ObsidianSource(vault_path=source_path, ledger_path=ledger_path)
        )

    # Ingesta one-shot (degradación suave por fuente: harvest_source retorna 0).
    counts = {}
    if source == "--all":
        for st in GENESIS_SOURCES:
            counts[st] = genesis_harvester.harvest_source(st)
    else:
        counts[source] = genesis_harvester.harvest_source(source)

    # F1.3 — snapshot de arquitectura (artefacto estático, no causal).
    from causadb._genesis_codebase import generate_codebase_snapshot
    snapshot = generate_codebase_snapshot(source_path, project_id=project_id)
    _write_codebase_snapshot_event(ledger_path, snapshot)

    # Resumen de la ingesta.
    summary = _write_genesis_summary(ledger_path, project_id, counts)

    return {
        "project_id": project_id,
        "sources": counts,
        "events_imported": sum(counts.values()),
        "codebase_generator": snapshot["generator"],
        "summary": summary,
    }


def cmd_genesis(args) -> tuple:
    """Handler CLI: ``causadb genesis import --source <name>|--all [--ledger]``."""
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))
    source = getattr(args, "source", "--all") or "--all"
    source_path = getattr(args, "path", None)
    result = run_genesis_import(ledger, source=source, source_path=source_path)
    return (0, json.dumps(result, sort_keys=True))