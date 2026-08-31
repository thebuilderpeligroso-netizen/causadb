"""HarvestSource — ActivityWatch.

Consulta la API REST de ActivityWatch (http://localhost:5600/api/0/) y produce
eventos ``TOOL_CALLED`` con ``app``, ``title`` y ``bucket_id``.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"last_timestamp": str}`` — último timestamp (ISO 8601) procesado.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from causadb._harvest_source import HarvestSource


class ActivityWatchSource(HarvestSource):
    """Fuente de harvest para ActivityWatch (REST API).

    Args:
        api_base: (opcional) URL base de la API. Default http://localhost:5600/api/0/
        ledger_path: (opcional) Aceptado por compatibilidad.
    """

    def __init__(self, api_base: str = "http://localhost:5600/api/0", ledger_path: Optional[str] = None):
        self.ledger_path = ledger_path
        self._api_base = api_base.rstrip("/")

    # -- Detección ----------------------------------------------------------

    def source_type(self) -> str:
        return "activitywatch"

    def cursor_key(self) -> str:
        return "activitywatch"

    def detect(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._api_base}/info", timeout=2) as response:
                return response.getcode() == 200
        except Exception:
            return False

    # -- Harvest ------------------------------------------------------------

    def harvest(self, cursor: Optional[dict] = None) -> list[dict]:
        last_timestamp = None
        if cursor is not None:
            last_timestamp = cursor.get("last_timestamp")

        events: list[dict] = []
        
        # 1. Obtener buckets
        buckets_url = f"{self._api_base}/buckets"
        try:
            with urllib.request.urlopen(buckets_url, timeout=5) as response:
                buckets = json.loads(response.read().decode())
        except Exception:
            return []

        # 2. Cosechar eventos por bucket
        for bucket_id, bucket in buckets.items():
            if bucket.get("type") != "afk" and bucket.get("type") != "window":
                continue
                
            events_url = f"{self._api_base}/buckets/{bucket_id}/events"
            try:
                with urllib.request.urlopen(events_url, timeout=5) as response:
                    bucket_events = json.loads(response.read().decode())
                    
                for event in bucket_events:
                    timestamp = event.get("timestamp")
                    if last_timestamp and timestamp <= last_timestamp:
                        continue
                        
                    data = event.get("data", {})
                    events.append({
                        "type": "TOOL_CALLED",
                        "timestamp": timestamp,
                        "app": data.get("app", "unknown"),
                        "title": data.get("title", "unknown"),
                        "bucket_id": bucket_id
                    })
            except Exception:
                continue
        
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        last_ts = cursor.get("last_timestamp")
        if harvested_raw_events:
            last_ts = harvested_raw_events[-1].get("timestamp", last_ts)
        return {"last_timestamp": last_ts}
