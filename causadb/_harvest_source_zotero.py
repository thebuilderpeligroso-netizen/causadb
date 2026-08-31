"""HarvestSource — Zotero API.

Consulta la API REST de Zotero (http://127.0.0.1:23123/api/items) y produce
eventos ``CONTEXT_UPDATED`` con ``title``, ``item_type`` y ``key``.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"last_item_key": str}`` — key del último item procesado.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from causadb._harvest_source import HarvestSource


class ZoteroSource(HarvestSource):
    """Fuente de harvest para Zotero (REST API).

    Args:
        api_base: URL base de la API Zotero (default http://127.0.0.1:23123/api).
        ledger_path: (opcional) Aceptado por compatibilidad.
    """

    def __init__(self, api_base: str = "http://127.0.0.1:23123/api", ledger_path: Optional[str] = None):
        self.ledger_path = ledger_path
        self._api_base = api_base.rstrip("/")

    def source_type(self) -> str:
        return "zotero"

    def cursor_key(self) -> str:
        return "zotero_items"

    def detect(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._api_base}/items", timeout=2) as response:
                return response.getcode() == 200
        except Exception:
            return False

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        last_key = cursor.get("last_item_key") if cursor else None
        
        events: list[dict] = []
        
        # Consultar items
        url = f"{self._api_base}/items"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                items = json.loads(response.read().decode())
        except Exception:
            return []

        for item in items:
            key = item.get("key")
            if last_key and key <= last_key:
                continue
                
            data = item.get("data", {})
            events.append({
                "type": "CONTEXT_UPDATED",
                "timestamp": item.get("dateModified", ""), # Simplificación
                "title": data.get("title", "unknown"),
                "item_type": data.get("itemType", "unknown"),
                "key": key
            })
            
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        last_key = cursor.get("last_item_key")
        if harvested_raw_events:
            last_key = harvested_raw_events[-1].get("key", last_key)
        return {"last_item_key": last_key}
