import json
import os
import re
import logging
from typing import Optional

from causadb._blob_store import BlobStore, resolve_payload

CHRONICLE_INDEX_VERSION = 1

# GAP-02 — regex de prosa: cita de event_id en el bloque Referencias de un
# BIT. El lookbehind negativo excluye campos que CONTIENEN "event_id"
# (genesis_event_id, parent_event_id, target_event_id, event_ids — la "s"
# final tampoco pasa el char class). Acepta UUID v4 y 32-hex (event_ids
# determinísticos del harvester).
_PROSE_EVENT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])event_id[\s:`]*`?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})`?"
)

# EventTypes cuyo payload.bit_id enlaza por AUTORIDAD DEL LEDGER (GAP-02).
_LEDGER_AUTHORITY_TYPES = frozenset({"CHRONICLE_ENTRY", "GOVERNANCE_DECISION"})


def index_path(ledger_path: str) -> str:
    """chronicle_index.json al lado del ledger."""
    return os.path.join(os.path.dirname(ledger_path), "chronicle_index.json")


def resolve_chronicle_path(ledger_path: str, explicit: Optional[str] = None) -> Optional[str]:
    """Resuelve el path del Chronicle (GAP-02; plan §4.1, orden (a)-(d)).

    Orden:
      (d) flag explícito ``--chronicle-path`` (si el archivo NO existe →
          None, FAIL-CLOSED);
      (a) ``config.json`` del dirname(ledger) → key ``chronicle_path``;
      (b) ``dirname(ledger)/CAUSADB_CHRONICLE.md``;
      (c) walk-up desde dirname(ledger) buscando ``<ancestor>/causadb/
          CAUSADB_CHRONICLE.md`` (realidad productiva: el Chronicle vive en
          ``Master/causadb/CAUSADB_CHRONICLE.md`` mientras el ledger está en
          ``Master/.causadb/``).

    Retorna None si nada existe (el caller decide el FAIL-CLOSED).
    """
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    ledger_dir = os.path.dirname(os.path.abspath(ledger_path))
    # (a) config.json del workspace
    cfg = os.path.join(ledger_dir, "config.json")
    try:
        with open(cfg) as f:
            data = json.load(f)
        cp = data.get("chronicle_path") if isinstance(data, dict) else None
        if isinstance(cp, str) and cp and os.path.isfile(cp):
            return cp
    except (OSError, json.JSONDecodeError):
        pass
    # (b) al lado del ledger
    direct = os.path.join(ledger_dir, "CAUSADB_CHRONICLE.md")
    if os.path.isfile(direct):
        return direct
    # (c) walk-up: <ancestor>/causadb/CAUSADB_CHRONICLE.md
    current = ledger_dir
    while True:
        candidate = os.path.join(current, "causadb", "CAUSADB_CHRONICLE.md")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _add_link(index: dict, bit_name: str, eid: str) -> None:
    """Enlaza eid ↔ bit en ambas direcciones (dedup)."""
    entry = index["by_bit"].setdefault(bit_name, {"event_ids": [], "description": ""})
    if eid not in entry["event_ids"]:
        entry["event_ids"].append(eid)
    if eid not in index["by_event"]:
        index["by_event"][eid] = []
    if bit_name not in index["by_event"][eid]:
        index["by_event"][eid].append(bit_name)


def rebuild_index(ledger_path: str, chronicle_path: Optional[str] = None) -> dict:
    """Regenera el índice desde cero (GAP-02).

    Fuentes, en orden de autoridad:
      1. **Ledger** (autoridad): escaneo completo; los eventos
         ``CHRONICLE_ENTRY``/``GOVERNANCE_DECISION`` con ``payload.bit_id``
         enlazan su event_id al BIT (nunca se pisan).
      2. **Prosa del Chronicle** (secundaria): por BIT, el bloque
         ``**Referencias:**`` se acota hasta el próximo bloque bold; los
         event_id citados (UUID o 32-hex) se enlazan SOLO si existen en el
         ledger (validación contra ``ledger_ids`` — no se enlazan fantasmas).

    FAIL-CLOSED (plan §4.1): sin Chronicle en ninguna ubicación →
    ``FileNotFoundError`` (el caller CLI lo convierte en exit≠0; ``load_index``
    lo captura y devuelve índice vacío — nunca crashea, Artículo V).
    """
    chronicle_path = resolve_chronicle_path(ledger_path, chronicle_path)
    if chronicle_path is None:
        raise FileNotFoundError(
            f"CAUSADB_CHRONICLE.md no encontrado para ledger {ledger_path} "
            "(FAIL-CLOSED: no se puede reconstruir el índice sin el chronicle; "
            "pasarlo explícito con --chronicle-path)"
        )
    index = {"version": CHRONICLE_INDEX_VERSION, "by_bit": {}, "by_event": {}}
    ledger_ids: set[str] = set()

    # -- 1. ledger como autoridad -------------------------------------------
    with open(ledger_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = entry.get("event") if isinstance(entry, dict) else None
            if not isinstance(event, dict):
                continue
            eid = event.get("event_id")
            if not eid:
                continue
            ledger_ids.add(eid)
            etype = event.get("event_type")
            raw_payload = event.get("payload") or {}
            # Resolve $blob payloads (GAP-02 fix del Checker): el MCP escribe
            # payloads grandes como $blob → sin resolución, el bit_id se
            # pierde y el enlace por autoridad del ledger no ocurre.
            blob_dir = os.path.join(os.path.dirname(ledger_path), "blobs")
            payload = resolve_payload(raw_payload, BlobStore(blob_dir))
            if etype in _LEDGER_AUTHORITY_TYPES and isinstance(payload, dict):
                bit_id = payload.get("bit_id")
                if isinstance(bit_id, str) and bit_id:
                    _add_link(index, bit_id, eid)

    # -- 2. prosa del chronicle (fuente secundaria) -------------------------
    with open(chronicle_path, "r") as f:
        content = f.read()
    for match in re.finditer(r"^## BIT-([\w.]+)", content, re.MULTILINE):
        bit_name = "BIT-" + match.group(1)
        # El bloque empieza DESPUÉS de la línea del header: la descripción
        # no puede ser el resto del título (ej. "— GAP-01").
        line_end = content.find("\n", match.end())
        if line_end == -1:
            line_end = match.end()
        block_end = content.find("\n## ", line_end)
        if block_end == -1:
            block_end = len(content)
        block = content[line_end:block_end]
        # descripción: primera línea no vacía tras el header
        desc = ""
        for line in block.split("\n"):
            if line.strip():
                desc = line.strip().lstrip("—–- ").strip()
                break
        entry = index["by_bit"].setdefault(
            bit_name, {"event_ids": [], "description": desc}
        )
        entry["description"] = desc
        # bloque Referencias, acotado hasta el próximo \n\*\* (o fin de BIT)
        ref_marker = block.find("**Referencias:**")
        if ref_marker != -1:
            ref_block = block[ref_marker:]
            ref_end = ref_block.find("\n**")
            if ref_end != -1:
                ref_block = ref_block[:ref_end]
            for m in _PROSE_EVENT_ID_RE.finditer(ref_block):
                eid = m.group(1)
                if eid in ledger_ids:
                    _add_link(index, bit_name, eid)

    save_index(ledger_path, index)
    return index


def _rebuild_or_empty(ledger_path: str) -> dict:
    """rebuild_index con captura del FAIL-CLOSED (FileNotFoundError).

    ``load_index`` NUNCA crashea (Artículo V): sin chronicle → índice vacío
    (los tests existentes test_load_corrupt_json/test_version_mismatch
    dependen de este comportamiento).
    """
    try:
        return rebuild_index(ledger_path)
    except FileNotFoundError:
        return {"version": CHRONICLE_INDEX_VERSION, "by_bit": {}, "by_event": {}}


def load_index(ledger_path: str) -> dict:
    """Retorna dict vacío si no existe o corrupto — NUNCA crashea (Artículo V)."""
    path = index_path(ledger_path)
    if not os.path.exists(path):
        return {"version": CHRONICLE_INDEX_VERSION, "by_bit": {}, "by_event": {}}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if data.get("version") != CHRONICLE_INDEX_VERSION:
            logging.warning(f"chronicle_index version mismatch: {data.get('version')} != {CHRONICLE_INDEX_VERSION}, rebuilding")
            return _rebuild_or_empty(ledger_path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"Could not load chronicle_index: {e}, rebuilding")
        return _rebuild_or_empty(ledger_path)


def save_index(ledger_path: str, index: dict) -> None:
    """Atomic write: write to tmp, rename, fsync dir."""
    path = index_path(ledger_path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # fsync directorio
    dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def link_events(ledger_path: str, bit_name: str, event_ids: list[str]) -> dict:
    """Vincular eventos a un BIT. Retorna el index actualizado."""
    index = load_index(ledger_path)
    if bit_name not in index["by_bit"]:
        index["by_bit"][bit_name] = {"event_ids": [], "description": ""}
    for eid in event_ids:
        if eid not in index["by_bit"][bit_name]["event_ids"]:
            index["by_bit"][bit_name]["event_ids"].append(eid)
        if eid not in index["by_event"]:
            index["by_event"][eid] = []
        if bit_name not in index["by_event"][eid]:
            index["by_event"][eid].append(bit_name)
    save_index(ledger_path, index)
    return index


def unlink_events(ledger_path: str, bit_name: str, event_ids: list[str]) -> dict:
    """Desvincular eventos de un BIT."""
    index = load_index(ledger_path)
    if bit_name in index["by_bit"]:
        index["by_bit"][bit_name]["event_ids"] = [
            eid for eid in index["by_bit"][bit_name]["event_ids"] if eid not in event_ids
        ]
    for eid in event_ids:
        if eid in index["by_event"]:
            index["by_event"][eid] = [b for b in index["by_event"][eid] if b != bit_name]
            if not index["by_event"][eid]:
                del index["by_event"][eid]
    save_index(ledger_path, index)
    return index


def query_by_bit(ledger_path: str, bit_name: str) -> list:
    """Retorna lista de event_ids para un BIT."""
    index = load_index(ledger_path)
    entry = index["by_bit"].get(bit_name)
    return entry["event_ids"] if entry else []


def query_by_event(ledger_path: str, event_id: str) -> list:
    """Retorna lista de BIT names para un event_id."""
    index = load_index(ledger_path)
    return index["by_event"].get(event_id, [])


def list_entries(ledger_path: str) -> list:
    """Retorna lista de {bit_name, event_count, description}."""
    index = load_index(ledger_path)
    return [
        {"bit_name": k, "event_count": len(v["event_ids"]), "description": v.get("description", "")}
        for k, v in sorted(index["by_bit"].items())
    ]