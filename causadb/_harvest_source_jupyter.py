"""HarvestSource — Jupyter notebooks (J.4).

Lee archivos ``.ipynb`` de un directorio (por defecto ``~/jupyter/`` o
path configurable) y produce eventos ``COMMAND_RUN`` por cada celda
procesada (celdas ``code`` y ``markdown``).

Formato .ipynb: JSON estándar de Jupyter Notebook v4 con estructura::

    {
      "cells": [
        {"cell_type": "code", "source": ["line1\\n", "line2"]},
        {"cell_type": "markdown", "source": ["# Title\\n"]}
      ],
      "metadata": {...},
      "nbformat": 4
    }

El campo ``source`` puede ser un string o una lista de strings (cada
uno típicamente terminado en ``\\n``); se concatenan.

Cursor:
    ``{"notebooks": [{"path": "...", "cells_processed": N}]}`` —
    registra cuántas celdas se procesaron de cada notebook. En la
    siguiente pasada, se salta esas celdas y procesa las nuevas (o
    re-procesa si el archivo cambió — heurística: si el mtime cambió,
    se recomienza desde 0).

Sin dependencias externas — solo stdlib (``os``, ``json``, ``glob``).
"""

import os
import json
import glob

from causadb._harvest_source import HarvestSource


class JupyterHarvestSource(HarvestSource):
    """Fuente de harvest para Jupyter notebooks.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        source_path: (opcional) Directorio con notebooks. Por defecto
            ``~/jupyter/``. Si no existe, ``detect()`` retorna False.
    """

    def __init__(self, ledger_path: str, source_path: str | None = None):
        super().__init__(ledger_path)
        self.source_path = source_path or os.path.expanduser("~/jupyter/")

    def source_type(self) -> str:
        return "jupyter"

    def cursor_key(self) -> str:
        return "jupyter_notebooks"

    def detect(self) -> bool:
        """True si el directorio existe y contiene al menos un .ipynb."""
        if not os.path.isdir(self.source_path):
            return False
        notebooks = glob.glob(os.path.join(self.source_path, "*.ipynb"))
        return len(notebooks) > 0

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        """Cosecha celdas de notebooks .ipynb.

        Resume desde el cursor ``{"notebooks": [...]}``. Para cada
        notebook, salta las celdas ya procesadas (por índice) a menos
        que el mtime del archivo haya cambiado (en cuyo caso recomienza
        desde la celda 0).
        """
        if not self.detect():
            return []

        cursor = cursor or {}
        notebooks_cursor = cursor.get("notebooks", [])
        # Indexar cursor por path relativo para lookup rápido
        cursor_map = {entry["path"]: entry for entry in notebooks_cursor}

        notebooks = sorted(glob.glob(os.path.join(self.source_path, "*.ipynb")))
        events: list[dict] = []

        for nb_path in notebooks:
            rel_path = os.path.relpath(nb_path, self.source_path)
            current_mtime = os.path.getmtime(nb_path)

            entry = cursor_map.get(rel_path)
            if entry and entry.get("mtime") == current_mtime:
                start_cell = entry.get("cells_processed", 0)
            else:
                # Archivo nuevo o modificado → procesar desde 0
                start_cell = 0

            cells_events = self._parse_notebook(nb_path, rel_path, start_cell)
            events.extend(cells_events)

        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        old_notebooks = cursor.get("notebooks", [])
        notebook_map = {entry["path"]: entry for entry in old_notebooks}

        harvested_by_notebook: dict[str, list[dict]] = {}
        for ev in harvested_raw_events:
            nb = ev.get("notebook")
            if nb:
                harvested_by_notebook.setdefault(nb, []).append(ev)

        new_notebooks = []
        for nb_path, events in harvested_by_notebook.items():
            old_entry = notebook_map.get(nb_path, {})
            max_cell = max(ev.get("cell_index", -1) for ev in events)
            mtime = old_entry.get(
                "mtime",
                os.path.getmtime(os.path.join(self.source_path, nb_path + ".ipynb"))
                if self.source_path else 0,
            )
            new_notebooks.append({
                "path": nb_path,
                "cells_processed": max_cell + 1,
                "mtime": mtime,
            })

        for entry in old_notebooks:
            if entry["path"] not in harvested_by_notebook:
                new_notebooks.append(entry)

        return {"notebooks": new_notebooks}

    # -- Internal -----------------------------------------------------------

    def _parse_notebook(
        self, nb_path: str, rel_path: str, start_cell: int
    ) -> list[dict]:
        """Parsea un .ipynb y produce COMMAND_RUN desde ``start_cell``."""
        try:
            with open(nb_path, "r", encoding="utf-8") as f:
                nb = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

        cells = nb.get("cells", [])
        events: list[dict] = []
        notebook_name = os.path.splitext(os.path.basename(nb_path))[0]

        for idx, cell in enumerate(cells):
            if idx < start_cell:
                continue
            cell_type = cell.get("cell_type", "code")
            source = cell.get("source", "")
            cell_source = self._normalize_source(source)

            events.append({
                "type": "COMMAND_RUN",
                "timestamp": self._cell_timestamp(cell, nb_path),
                "command": cell_source,  # campo canónico COMMAND_RUN
                "cell_source": cell_source,
                "notebook": notebook_name,
                "cell_type": cell_type,
                "cell_index": idx,
            })

        return events

    @staticmethod
    def _normalize_source(source) -> str:
        """Normaliza el campo ``source`` de una celda a string.

        Jupyter permite que ``source`` sea un string o una lista de
        strings (cada uno típicamente terminado en ``\\n``).
        """
        if isinstance(source, str):
            return source
        if isinstance(source, list):
            return "".join(source)
        return str(source)

    @staticmethod
    def _cell_timestamp(cell: dict, nb_path: str) -> str:
        """Extrae timestamp de una celda si está disponible, sino usa
        el mtime del archivo como fallback aproximado."""
        # Jupyter notebooks v4 no siempre tienen timestamp por celda,
        # pero algunas extensiones lo agregan en metadata.
        metadata = cell.get("metadata", {})
        if "timestamp" in metadata:
            return str(metadata["timestamp"])
        # Fallback: mtime del archivo como timestamp aproximado
        try:
            import datetime
            mtime = os.path.getmtime(nb_path)
            dt = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except OSError:
            return ""
