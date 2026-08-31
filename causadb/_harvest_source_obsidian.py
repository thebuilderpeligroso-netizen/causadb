"""HarvestSource — Obsidian vault.

Lee archivos ``.md`` de un directorio y produce eventos ``FILE_MODIFIED``
por cada archivo modificado.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"files": {"/path/to/file.md": mtime_float}}`` — seguimiento de mtime
por archivo.
"""

from __future__ import annotations

import os
import glob
from typing import Optional

from causadb._harvest_source import HarvestSource


class ObsidianSource(HarvestSource):
    """Fuente de harvest para Obsidian vault.

    Args:
        vault_path: Ruta al directorio del vault.
        ledger_path: (opcional) Aceptado por compatibilidad.
    """

    def __init__(self, vault_path: str, ledger_path: Optional[str] = None):
        self.ledger_path = ledger_path
        self.vault_path = vault_path

    def source_type(self) -> str:
        return "obsidian"

    def cursor_key(self) -> str:
        return "obsidian_vault"

    def detect(self) -> bool:
        if not os.path.isdir(self.vault_path):
            return False
        md_files = glob.glob(os.path.join(self.vault_path, "**/*.md"), recursive=True)
        return len(md_files) > 0

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        cursor = cursor or {}
        files_cursor = cursor.get("files", {})
        
        md_files = glob.glob(os.path.join(self.vault_path, "**/*.md"), recursive=True)
        events: list[dict] = []
        new_cursor_files = files_cursor.copy()

        for md_path in md_files:
            mtime = os.path.getmtime(md_path)
            last_mtime = files_cursor.get(md_path, 0)
            
            if mtime > last_mtime:
                # Archivo modificado o nuevo
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        first_line = f.readline().strip()
                except OSError:
                    first_line = ""
                    
                events.append({
                    "type": "FILE_MODIFIED",
                    "timestamp": mtime,
                    "path": md_path,
                    "size": os.path.getsize(md_path),
                    "first_line": first_line
                })
                new_cursor_files[md_path] = mtime
        
        # Actualizar cursor (se hace externamente en harvester, pero
        # devolvemos el dict actualizado para debug)
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        files = dict(cursor.get("files", {}))
        for ev in harvested_raw_events:
            path = ev.get("path")
            timestamp = ev.get("timestamp")
            if path and timestamp is not None:
                files[path] = timestamp
        return {"files": files}
