"""HarvestSource — Browser history.

Lee bases de datos SQLite de historial de Chrome (~/.config/google-chrome/Default/History)
y Firefox (~/.mozilla/firefox/*.default/places.sqlite) y produce eventos
``OBSERVATION`` con ``url``, ``title`` y ``visit_time``.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"last_visit_time": int}`` — último timestamp procesado (formato Chrome).
"""

from __future__ import annotations

import os
import sqlite3
import shutil
import tempfile
from typing import Optional

from causadb._harvest_source import HarvestSource


class BrowserHistorySource(HarvestSource):
    """Fuente de harvest para historial de navegador (Chrome/Firefox).

    Args:
        browser_paths: (opcional) Lista de rutas a los archivos SQLite de historial.
            Si es None, se intentan autodetectar las rutas comunes.
        ledger_path: (opcional) Aceptado por compatibilidad.
    """

    def __init__(self, browser_paths: Optional[list[str]] = None, ledger_path: Optional[str] = None):
        self.ledger_path = ledger_path
        self._browser_paths = browser_paths

    # -- Detección ----------------------------------------------------------

    def _get_default_paths(self) -> list[str]:
        """Intenta autodetectar rutas de historial comunes."""
        home = os.path.expanduser("~")
        paths = []
        
        # Chrome
        chrome_path = os.path.join(home, ".config/google-chrome/Default/History")
        if os.path.exists(chrome_path):
            paths.append(chrome_path)
            
        # Firefox (busca en profile por defecto)
        ff_path = os.path.join(home, ".mozilla/firefox")
        if os.path.exists(ff_path):
            for entry in os.listdir(ff_path):
                if entry.endswith(".default"):
                    ff_db = os.path.join(ff_path, entry, "places.sqlite")
                    if os.path.exists(ff_db):
                        paths.append(ff_db)
        return paths

    def source_type(self) -> str:
        return "browser"

    def cursor_key(self) -> str:
        return "browser_history"

    def detect(self) -> bool:
        paths = self._browser_paths or self._get_default_paths()
        return len(paths) > 0

    # -- Harvest ------------------------------------------------------------

    def harvest(self, cursor: Optional[dict] = None) -> list[dict]:
        paths = self._browser_paths or self._get_default_paths()
        last_visit_time = 0
        if cursor is not None:
            last_visit_time = cursor.get("last_visit_time", 0)

        events: list[dict] = []
        for path in paths:
            # SQLite DB puede estar bloqueada, copiar a tmp
            with tempfile.NamedTemporaryFile() as tmp:
                shutil.copy2(path, tmp.name)
                
                conn = sqlite3.connect(tmp.name)
                cursor_db = conn.cursor()
                
                try:
                    if "History" in path: # Chrome
                        query = """SELECT url, title, last_visit_time 
                                   FROM urls 
                                   WHERE last_visit_time > ? 
                                   ORDER BY last_visit_time ASC"""
                    else: # Firefox
                        query = """SELECT url, title, visit_date 
                                   FROM moz_places 
                                   WHERE visit_date > ? 
                                   ORDER BY visit_date ASC"""
                    
                    cursor_db.execute(query, (last_visit_time,))
                    for url, title, visit_time in cursor_db.fetchall():
                        events.append({
                            "type": "OBSERVATION",
                            "timestamp": visit_time, # Harvester normalizará esto
                            "url": url,
                            "title": title,
                            "visit_time": visit_time,
                            "severity": "info"
                        })
                except sqlite3.Error:
                    continue
                finally:
                    conn.close()
        
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        last_visit = cursor.get("last_visit_time", 0)
        if harvested_raw_events:
            last_visit = max(last_visit, max(e.get("visit_time", 0) for e in harvested_raw_events))
        return {"last_visit_time": last_visit}
