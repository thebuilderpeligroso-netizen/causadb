"""`causadb undo` — restaurar archivo desde último snapshot en BlobStore.

Fase 8.4 — Workspace Self-Protection.

Flujo:
1. Resolver ledger_path vía resolve_ledger(args.ledger)
2. Leer el ledger línea por línea para encontrar eventos FILE_MODIFIED
   que referencien el archivo solicitado
3. Tomar el último evento (más reciente)
4. Buscar el contenido en BlobStore (recorriendo blobs para encontrar
   el que coincide con el path del archivo)
5. Si no se encuentra en BlobStore, buscar en snapshots
6. Escribir el contenido al archivo en disco
7. Si no hay snapshot previo, reportar "no snapshot available"
"""

import hashlib
import json
import os
from typing import Tuple, List, Optional

from causadb._workspace import resolve_ledger
from causadb._config import CausaDBConfig
from causadb._blob_store import BlobStore, resolve_payload


def cmd_undo(args) -> Tuple[int, str]:
    """Restaura un archivo desde el último snapshot conocido en el ledger.

    Args:
        args: Namespace con ``file`` (ruta al archivo a restaurar) y
              ``ledger`` (ruta al ledger, opcional).

    Returns:
        (exit_code, output_json_string)
    """
    file_path = args.file

    # 1. Resolver ledger_path
    try:
        ledger_path = resolve_ledger(args.ledger)
    except Exception as e:
        return (1, json.dumps({
            "status": "error",
            "error": f"Could not resolve ledger: {e}",
        }))

    # 2. Leer el ledger para encontrar eventos FILE_MODIFIED para este archivo
    file_events = _find_file_events(ledger_path, file_path)

    if not file_events:
        return (1, json.dumps({
            "status": "error",
            "error": f"No FILE_MODIFIED events found for '{file_path}' in ledger",
        }))

    # 3. Seleccionar el "last known good": el estado ANTES de la última
    #    modificación. Saltamos el último evento (estado actual/broken) y
    #    caminamos hacia atrás saltando eventos 'deleted'. Los eventos ya
    #    vienen ordenados por timestamp ascendente.
    if len(file_events) < 2:
        return (1, json.dumps({
            "status": "error",
            "error": f"Only one FILE_MODIFIED event found for '{file_path}' — "
                     f"no previous snapshot to restore to",
        }))

    restore_event = None

    # Híbrido: si el archivo existe en disco, elegir el ÚLTIMO evento cuyo
    # content_hash difiera del hash del contenido ACTUAL en disco. Esto
    # detecta el "último cambio real" (incluye si el archivo se tocó a mano
    # después del último evento registrado). Los eventos con content_hash
    # None no son comparables y se saltan; los 'deleted' también.
    disk_hash = _current_disk_hash(file_path)
    if disk_hash is not None:
        for ev in reversed(file_events):
            if ev.get("action") == "deleted":
                continue
            ev_hash = ev.get("content_hash")
            if ev_hash is None:
                continue  # no comparable; seguir caminando hacia atrás
            if ev_hash != disk_hash:
                restore_event = ev
                break

    # Fallback: comportamiento actual (penúltimo no-deleted). Se usa cuando
    # el archivo no existe en disco, o ningún evento es comparable, o no se
    # encontró ninguno que difiera del disco.
    if restore_event is None:
        for ev in reversed(file_events[:-1]):  # skip the current (last) event
            if ev.get("action") == "deleted":
                continue
            restore_event = ev
            break

    if restore_event is None:
        return (1, json.dumps({
            "status": "error",
            "error": f"No snapshot available for '{file_path}'. "
                     f"No previous non-deleted FILE_MODIFIED event found.",
        }))

    action = restore_event.get("action", "unknown")

    # 4. Buscar contenido en BlobStore
    config = CausaDBConfig(ledger_path=ledger_path)
    content = None

    if config.blob_store_enabled:
        # Buscar el blob que coincide con el content_hash del evento
        content_hash = restore_event.get("content_hash")
        content = _find_content_in_blob_store(config.blob_store_path, file_path, content_hash)

    # 5. Si no se encontró en BlobStore, buscar en snapshots
    if content is None:
        content = _find_content_in_snapshots(ledger_path, config, file_path)

    if content is None:
        return (1, json.dumps({
            "status": "error",
            "error": f"No snapshot available for '{file_path}'. "
                     f"BlobStore may not have been active when the file was last modified.",
        }))

    # 6. Escribir el contenido al archivo en disco
    try:
        abs_file_path = file_path if os.path.isabs(file_path) else os.path.abspath(file_path)
        parent_dir = os.path.dirname(abs_file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(abs_file_path, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        return (1, json.dumps({
            "status": "error",
            "error": f"Failed to write restored file: {e}",
        }))

    return (0, json.dumps({
        "status": "ok",
        "file": abs_file_path,
        "restored_from": "blob_store",
    }))


def _current_disk_hash(file_path: str) -> Optional[str]:
    """SHA-256 del contenido ACTUAL del archivo en disco (modo binario).

    Lee los bytes crudos (como el harvester, ``_harvest_source_filesystem.py``)
    para que el hash sea comparable con el ``content_hash`` de los eventos.
    Retorna None si el archivo no existe en disco o no se puede leer.
    """
    abs_path = file_path if os.path.isabs(file_path) else os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return None
    try:
        with open(abs_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _paths_match(requested_path: str, event_path: str) -> bool:
    """Determina si dos rutas referencian el mismo archivo.

    Hace match por:
    - igualdad exacta
    - sufijo con boundary de directorio (``requested`` termina en
      ``/<event_path>`` o viceversa), evitando falsos positivos como
      ``xtests/test_watch.py`` para ``tests/test_watch.py``
    - basename como último recurso (mismo nombre de archivo)

    El basename fallback puede producir falsos positivos para archivos
    con el mismo nombre en directorios distintos, pero es seguro en la
    práctica porque la búsqueda de contenido se desambigua por
    ``content_hash`` (solo se restaura el blob cuyo hash coincide).

    Esto resuelve el bug DEUDA-UNDO: el mismo archivo puede estar
    trackeado bajo ``tests/x.py`` y ``causadb/tests/x.py`` (re-root del
    workspace), y ambos comparten el sufijo ``/tests/x.py``.
    """
    if not event_path:
        return False
    if requested_path == event_path:
        return True
    # Suffix match component-boundary-aware
    if requested_path.endswith("/" + event_path):
        return True
    if event_path.endswith("/" + requested_path):
        return True
    # Basename fallback (desambiguado por content_hash en la búsqueda)
    return os.path.basename(requested_path) == os.path.basename(event_path)


def _find_file_events(ledger_path: str, file_path: str) -> list[dict]:
    """Lee el ledger y retorna todos los eventos FILE_MODIFIED para un archivo.

    Usa ``_paths_match`` (sufijo por boundary + basename) para que
    matcheen tanto paths prefixed como relativos. Ordena por timestamp
    ascendente (append order es el tiebreak).
    """
    events = []
    try:
        f = open(ledger_path, "r")
    except (OSError, FileNotFoundError):
        return events

    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = entry.get("event", {})
            if event.get("event_type") != "FILE_MODIFIED":
                continue

            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue

            ev_path = payload.get("path", "")
            if _paths_match(file_path, ev_path):
                events.append({
                    "path": ev_path,
                    "action": payload.get("action", "unknown"),
                    "content_hash": payload.get("content_hash"),
                    "timestamp": event.get("timestamp"),
                })

    # Sort por timestamp ascendente (append order como tiebreak estable)
    events.sort(key=lambda e: e.get("timestamp") or "")
    return events


def _find_content_in_blob_store(blob_store_path: str, file_path: str, content_hash: str | None = None) -> str | None:
    """Busca el contenido de un archivo en el BlobStore recorriendo todos los blobs.

    Si se provee content_hash, busca el blob cuyo contenido (al hashearlo con SHA-256)
    coincida con content_hash. Si no, retorna el primer blob que coincida por path.

    Returns:
        El contenido del archivo como string, o None si no se encuentra.
    """

    if not os.path.isdir(blob_store_path):
        return None

    file_basename = os.path.basename(file_path)

    try:
        blob_store = BlobStore(blob_store_path)
    except Exception:
        return None

    # Recorrer todos los blobs en el directorio
    for root, dirs, files in os.walk(blob_store_path):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            blob_hash = fname.replace(".json", "")
            try:
                blob_data = blob_store.get(blob_hash)
            except (FileNotFoundError, json.JSONDecodeError):
                continue

            blob_path = blob_data.get("path", "")
            if _paths_match(file_path, blob_path):
                blob_content = blob_data.get("content")
                if blob_content is None:
                    continue

                # Si tenemos content_hash, verificar que coincida
                if content_hash is not None:
                    computed_hash = hashlib.sha256(blob_content.encode("utf-8")).hexdigest()
                    if computed_hash == content_hash:
                        return blob_content
                    # Si no coincide, seguir buscando (puede haber múltiples versiones)
                    continue

                # Sin content_hash, retornar el primero que coincida
                return blob_content

    return None


def _find_content_in_snapshots(ledger_path: str, config: CausaDBConfig, file_path: str) -> str | None:
    """Busca el contenido de un archivo en snapshots del ledger.

    1. Recorre los eventos FILE_MODIFIED (de atrás hacia adelante) con
       ``pre_snapshot``/``post_snapshot`` (escritos por el Vigilante /
       auto-snapshot) y restaura el primero cuyo manifest contenga el
       archivo — ``pre_snapshot`` antes que ``post_snapshot`` porque es el
       estado previo a la modificación (el destino del undo).
    2. Como fallback secundario, recorre los eventos PROJECT_SNAPSHOT.
    """
    if not config.blob_store_enabled:
        return None

    try:
        blob_store = BlobStore(config.blob_store_path)
    except Exception:
        return None

    # Leer el ledger para encontrar snapshots
    try:
        f = open(ledger_path, "r")
    except (OSError, FileNotFoundError):
        return None

    file_basename = os.path.basename(file_path)

    with f:
        lines = f.readlines()

    # 1. FILE_MODIFIED events carry pre/post snapshots — newest first.
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        event = entry.get("event", {})
        if event.get("event_type") != "FILE_MODIFIED":
            continue

        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        payload = resolve_payload(payload, blob_store)
        if not isinstance(payload, dict):
            continue

        for snap_hash in (
            payload.get("pre_snapshot") or event.get("pre_snapshot"),
            payload.get("post_snapshot") or event.get("post_snapshot"),
        ):
            if not snap_hash:
                continue
            content = _content_from_snapshot_hash(
                snap_hash, blob_store, file_path, file_basename,
            )
            if content is not None:
                return content

    # 2. PROJECT_SNAPSHOT fallback.
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        event = entry.get("event", {})
        if event.get("event_type") != "PROJECT_SNAPSHOT":
            continue

        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        payload = resolve_payload(payload, blob_store)
        if not isinstance(payload, dict):
            continue

        snapshot_hash = payload.get("snapshot_hash")
        if not snapshot_hash:
            continue

        content = _content_from_snapshot_hash(
            snapshot_hash, blob_store, file_path, file_basename,
        )
        if content is not None:
            return content

    return None


def _content_from_snapshot_hash(
    snap_hash: str, blob_store: BlobStore, file_path: str, file_basename: str,
) -> str | None:
    """Extrae el contenido de *file_path* desde un manifest de snapshot."""
    try:
        snapshot = blob_store.get(snap_hash)
        files = snapshot.get("files", {})
        target_key = None
        for key in files:
            if _paths_match(file_path, key):
                target_key = key
                break

        if target_key is None:
            return None

        file_entry = files[target_key]
        blob_refs = snapshot.get("blob_refs", {})
        file_hash = file_entry.get("hash")
        blob_sha = blob_refs.get(file_hash)
        if blob_sha:
            content_blob = blob_store.get(blob_sha)
            import base64
            return base64.b64decode(content_blob["content_b64"]).decode("utf-8")
    except Exception:
        return None

    return None