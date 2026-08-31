"""HarvestSource — Shell history.

Lee ``~/.bash_history`` o ``~/.zsh_history`` y produce eventos
``COMMAND_RUN`` con ``payload.command`` y ``timestamp``.

Duck-typing: implementa la interfaz de ``HarvestSource`` sin heredar
estrictamente (la herencia se mantiene solo para compatibilidad con
el constructor base, pero los métodos se sobreescriben).

Cursor: ``{"line": N}`` — número de línea desde la cual empezar a leer.
"""

from __future__ import annotations

import os
from typing import Optional

from causadb._harvest_source import HarvestSource


class ShellHistorySource(HarvestSource):
    """Fuente de harvest para historial de shell (bash/zsh).

    Args:
        source_path: (opcional) Ruta al archivo de historial. Si es
            ``None``, se autodetecta entre ``~/.bash_history`` y
            ``~/.zsh_history`` (el primero que exista).
        ledger_path: (opcional) No usado por esta fuente, pero aceptado
            para compatibilidad con el constructor base de
            ``HarvestSource``.
    """

    def __init__(self, source_path: Optional[str] = None, ledger_path: Optional[str] = None):
        # No llamamos a super().__init__ con un ledger_path obligatorio
        # porque esta fuente no escribe al ledger directamente.
        self.ledger_path = ledger_path
        self._source_path = source_path

    # -- Detección ----------------------------------------------------------

    def _resolve_path(self) -> Optional[str]:
        """Resuelve la ruta del archivo de historial a usar."""
        if self._source_path is not None:
            return self._source_path
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".bash_history"),
            os.path.join(home, ".zsh_history"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def source_type(self) -> str:
        return "shell"

    def cursor_key(self) -> str:
        return "shell_history"

    def detect(self) -> bool:
        path = self._resolve_path()
        return path is not None and os.path.exists(path)

    # -- Harvest ------------------------------------------------------------

    def harvest(self, cursor: Optional[dict] = None) -> list[dict]:
        path = self._resolve_path()
        if path is None or not os.path.exists(path):
            return []

        start_line = 0
        if cursor is not None:
            start_line = int(cursor.get("line", 0))

        # mtime del archivo como fallback de timestamp
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0

        events: list[dict] = []
        with open(path, "r", errors="replace") as f:
            for idx, raw_line in enumerate(f):
                if idx < start_line:
                    continue
                command = raw_line.rstrip("\n")
                if not command.strip():
                    continue
                events.append({
                    "type": "COMMAND_RUN",
                    "timestamp": mtime,  # normalize_timestamp lo convierte
                    "command": command,
                })
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        current_line = cursor.get("line", 0)
        new_line = current_line + len(harvested_raw_events)
        return {"line": new_line}
