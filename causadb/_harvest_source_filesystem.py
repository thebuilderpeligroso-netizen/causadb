"""HarvestSource — Filesystem project watcher.
 
Recorre el directorio del proyecto y detecta archivos creados, modificados
o borrados comparando mtime + tamaño contra el cursor previo. Produce
eventos ``FILE_MODIFIED`` con ``path``, ``size`` y ``action``.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"files": {"rel/path": {"mtime": int, "size": int}}}`` —
snapshot de mtime + tamaño por archivo. El cursor se persiste entre ciclos
de harvest para que solo se cosechen los cambios.

Las fuentes que comparten cursor_key deben implementar ``advance_cursor``
con el formato correcto para su caso de uso. La base class ``HarvestSource``
define un default secuencial que las fuentes pueden sobrescribir.

Fase 8.3 — BlobStore integration: cuando ``config.blob_store_enabled`` es
True, cada archivo modificado/creado se persiste en BlobStore y el raw
event incluye ``content_hash`` (SHA-256 del contenido crudo) y ``$blob``
(hash del dict persistido en BlobStore, resoluble vía ``resolve_payload``).
Para archivos deleted, ``content_hash`` es null y ``$blob`` ausente.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from causadb._harvest_source import HarvestSource
from causadb._config import CausaDBConfig
from causadb._blob_store import BlobStore

# Tamaño máximo de archivo que se carga entero en RAM para hashear y
# persistir en BlobStore. Archivos por encima de este límite se registran
# solo con metadata (path/size/mtime) y content_hash=None, evitando OOM
# en el primer harvest de proyectos con archivos binarios grandes.
MAX_BLOB_SIZE = 10 * 1024 * 1024  # 10 MB

# Cantidad de bytes iniciales que se leen para detectar si un archivo es
# binario (presencia de bytes nulos). Si los primeros bytes contienen un
# byte nulo, se trata como binario y solo se guarda metadata.
_BINARY_SNIFF_BYTES = 2048

_DEFAULT_EXCLUDED_DIRS = {
    "__pycache__",
    "node_modules",
    ".git",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "dist",
    "build",
    "target",
    "eggs",
    ".eggs",
}


class FilesystemSource(HarvestSource):
    """Fuente de harvest para cambios de archivos en un directorio de proyecto.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        project_root: Ruta absoluta al directorio a monitorear.
        extra_excludes: Nombres de directorios adicionales a excluir.
        config: Configuración de CausaDB (opcional). Si se provee y
            ``blob_store_enabled`` es True, se persisten los contenidos
            de archivos en BlobStore.
    """

    def __init__(
        self,
        ledger_path: str,
        project_root: str,
        extra_excludes: Optional[list[str]] = None,
        config: Optional[CausaDBConfig] = None,
    ):
        super().__init__(ledger_path)
        self.project_root = project_root
        self._excluded_dirs = _DEFAULT_EXCLUDED_DIRS | set(
            extra_excludes or []
        )
        self._config = config
        self._blob_store: Optional[BlobStore] = None
        if config is not None and config.blob_store_enabled:
            self._blob_store = BlobStore(config.blob_store_path)
        self._snapshot_disabled = False
        self._prev_snapshot: Optional[dict] = None

    def source_type(self) -> str:
        return "filesystem"

    def cursor_key(self) -> str:
        return "filesystem_watch"

    def detect(self) -> bool:
        return os.path.isdir(self.project_root)

    def harvest(self, cursor: dict | None = None):
        cursor = cursor or {}
        prev_files: dict[str, dict] = cursor.get("files", {})

        current_files: dict[str, dict] = {}

        pre_snap_hash = self._take_pre_snapshot()

        raw_events: list[dict] = []
        try:
            for raw_event in self._walk(prev_files, current_files):
                raw_events.append(raw_event)
        except PermissionError:
            pass

        for relpath, prev_info in prev_files.items():
            if relpath not in current_files:
                raw_events.append({
                    "type": "FILE_MODIFIED",
                    "timestamp": prev_info["mtime"],
                    "path": relpath,
                    "size": 0,
                    "action": "deleted",
                    "content_hash": None,
                })

        post_snap_hash = self._take_post_snapshot()

        for raw_event in raw_events:
            raw_event["pre_snapshot"] = pre_snap_hash
            raw_event["post_snapshot"] = post_snap_hash
            yield raw_event

    def _take_pre_snapshot(self) -> Optional[str]:
        if self._snapshot_disabled or self._blob_store is None:
            return None

        from causadb._snapshot import WorkspaceSnapshot

        pre_snap = WorkspaceSnapshot.take(self.project_root, self._prev_snapshot)
        if pre_snap is None:
            self._snapshot_disabled = True
            return None

        try:
            pre_hash = WorkspaceSnapshot.store(
                pre_snap, self._blob_store, root_dir=self.project_root
            )
        except Exception:
            self._snapshot_disabled = True
            return None

        self._pre_snap_cache = pre_snap
        return pre_hash

    def _take_post_snapshot(self) -> Optional[str]:
        if self._snapshot_disabled or self._blob_store is None:
            return None

        from causadb._snapshot import WorkspaceSnapshot

        prev = getattr(self, "_pre_snap_cache", None)
        post_snap = WorkspaceSnapshot.take(self.project_root, prev)
        if post_snap is None:
            self._snapshot_disabled = True
            return None

        try:
            post_hash = WorkspaceSnapshot.store(
                post_snap, self._blob_store, root_dir=self.project_root
            )
        except Exception:
            post_hash = None

        self._prev_snapshot = post_snap
        return post_hash

    def _walk(self, prev_files, current_files):
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d not in self._excluded_dirs
            ]

            for fname in filenames:
                if fname.startswith(".") or fname.endswith(".pyc"):
                    continue

                fpath = os.path.join(dirpath, fname)
                relpath = os.path.relpath(fpath, self.project_root)

                try:
                    st = os.stat(fpath)
                except OSError:
                    continue

                mtime = int(st.st_mtime)
                size = st.st_size

                prev = prev_files.get(relpath)
                if prev is None:
                    action = "created"
                elif prev["mtime"] != mtime or prev["size"] != size:
                    action = "modified"
                else:
                    current_files[relpath] = {"mtime": mtime, "size": size}
                    continue

                content_hash = None
                blob_hash = None
                if self._blob_store is not None:
                    content_hash, blob_hash = self._maybe_capture_content(
                        fpath, relpath, size, mtime, action
                    )

                raw_event = {
                    "type": "FILE_MODIFIED",
                    "timestamp": mtime,
                    "path": relpath,
                    "size": size,
                    "action": action,
                }
                if self._blob_store is not None:
                    raw_event["content_hash"] = content_hash
                    if blob_hash is not None:
                        raw_event["$blob"] = blob_hash

                yield raw_event
                current_files[relpath] = {"mtime": mtime, "size": size}

    def _maybe_capture_content(
        self, fpath: str, relpath: str, size: int, mtime: int, action: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Captura el contenido de un archivo al BlobStore con size cap.

        Políticas anti-OOM (24GB detectado en primer harvest):
        - Si ``size > MAX_BLOB_SIZE``: NO se lee el contenido. Se retorna
          ``(None, None)`` (solo metadata) y se loguea un warning.
        - Si el archivo es binario (byte nulo en los primeros
          ``_BINARY_SNIFF_BYTES`` bytes): NO se persiste contenido al
          BlobStore. Se retorna ``(None, None)`` y se loguea un debug.
        - En caso de ``MemoryError`` durante la lectura: se atrapa y se
          retorna ``(None, None)`` (solo metadata).

        Returns:
            Tupla ``(content_hash, blob_hash)``: ``content_hash`` es el
            SHA-256 hex del contenido crudo; ``blob_hash`` es el hash del
            dict persistido en BlobStore (que incluye ``action`` y
            ``content_hash``). ``(None, None)`` si solo se registra
            metadata.
        """
        if size > MAX_BLOB_SIZE:
            logging.warning(
                "FilesystemSource: file %s (%d bytes) exceeds MAX_BLOB_SIZE "
                "(%d bytes); storing metadata only (content_hash=None)",
                relpath, size, MAX_BLOB_SIZE,
            )
            return None, None

        try:
            with open(fpath, "rb") as f:
                # Sniff inicial para detectar binarios sin cargar todo.
                sniff = f.read(_BINARY_SNIFF_BYTES)
                if b"\x00" in sniff:
                    logging.debug(
                        "FilesystemSource: binary file %s detected; "
                        "storing metadata only (content_hash=None)",
                        relpath,
                    )
                    return None, None
                # Archivo de texto: leer el resto y concatenar.
                rest = f.read()
                file_content = sniff + rest
        except MemoryError:
            logging.warning(
                "FilesystemSource: MemoryError reading %s (%d bytes); "
                "storing metadata only (content_hash=None)",
                relpath, size,
            )
            return None, None
        except (OSError, UnicodeDecodeError):
            return None, None

        content_hash = hashlib.sha256(file_content).hexdigest()
        blob_hash = self._blob_store.put({
            "path": relpath,
            "content": file_content.decode("utf-8", errors="replace"),
            "size": size,
            "mtime": mtime,
            "action": action,
            "content_hash": content_hash,
        })
        return content_hash, blob_hash

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        prev_files: dict[str, dict] = dict(cursor.get("files", {}))

        for ev in harvested_raw_events:
            path = ev.get("path")
            if path is None:
                continue
            if ev.get("action") == "deleted":
                prev_files.pop(path, None)
            else:
                prev_files[path] = {
                    "mtime": int(ev.get("timestamp", 0)),
                    "size": ev.get("size", 0),
                }

        return {"files": prev_files}
