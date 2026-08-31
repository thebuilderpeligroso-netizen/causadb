"""HarvestSource — Git reflog.

Ejecuta ``git reflog show --date=iso`` en el directorio actual (o en
un directorio configurado) y produce eventos ``COMMIT_MADE`` con
``payload.commit_hash`` y ``payload.message``.

Duck-typing: implementa la interfaz de ``HarvestSource``.

Cursor: ``{"commit": last_hash}`` — hash del último commit cosechado.
Si el cursor trae un hash, se skipean las entradas del reflog hasta
encontrar ese hash (exclusive), y se cosechan las siguientes.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from causadb._harvest_source import HarvestSource


class GitReflogSource(HarvestSource):
    """Fuente de harvest para git reflog.

    Args:
        source_path: (opcional) Directorio del repo git. Si es ``None``,
            usa el directorio actual (``os.getcwd()``).
        ledger_path: (opcional) No usado, aceptado por compatibilidad.
    """

    def __init__(self, source_path: Optional[str] = None, ledger_path: Optional[str] = None):
        self.ledger_path = ledger_path
        self._source_path = source_path

    def source_type(self) -> str:
        return "git"

    def cursor_key(self) -> str:
        return "git_reflog"

    # -- Detección ----------------------------------------------------------

    def _repo_dir(self) -> str:
        return self._source_path if self._source_path is not None else os.getcwd()

    def detect(self) -> bool:
        """True si ``git log -1`` funciona en el directorio del repo."""
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H"],
                cwd=self._repo_dir(),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    # -- Harvest ------------------------------------------------------------

    def _run_reflog(self) -> str:
        """Ejecuta ``git reflog show --date=iso`` y retorna stdout."""
        try:
            result = subprocess.run(
                ["git", "reflog", "show", "--date=iso"],
                cwd=self._repo_dir(),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return ""
            return result.stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    @staticmethod
    def _parse_reflog_line(line: str) -> Optional[dict]:
        """Parsea una línea de ``git reflog show --date=iso``.

        Formato real (verificado 2026-07-29)::

            abc1234 HEAD@{2026-07-29 14:31:55 -0300}: commit: msg
            abc1234 HEAD@{2026-07-29 14:31:55 -0300}: commit (initial): msg

        El timestamp va entre llaves ``{}`` después de ``HEAD@``, no entre
        paréntesis al final de la línea.

        Returns:
            dict con ``commit_hash``, ``message``, ``timestamp`` o
            ``None`` si la línea no se puede parsear.
        """
        if not line.strip():
            return None

        # Extraer timestamp entre { } después de HEAD@
        brace_open = line.find("{")
        brace_close = line.find("}", brace_open + 1) if brace_open != -1 else -1
        if brace_open == -1 or brace_close == -1 or brace_close < brace_open:
            return None
        timestamp = line[brace_open + 1:brace_close].strip()

        # rest = "<hash> HEAD@{...}: <msg>"  →  tomar después de "}:"
        after_brace = line[brace_close + 1:].lstrip()
        if after_brace.startswith(":"):
            after_brace = after_brace[1:].lstrip()

        parts = line.split(None, 1)
        if not parts:
            return None
        commit_hash = parts[0]
        message = after_brace.strip()

        return {
            "commit_hash": commit_hash,
            "message": message,
            "timestamp": timestamp,
        }

    def harvest(self, cursor: Optional[dict] = None) -> list[dict]:
        stdout = self._run_reflog()
        if not stdout:
            return []

        last_hash = None
        if cursor is not None:
            last_hash = cursor.get("commit")

        events: list[dict] = []
        skipping = last_hash is not None
        for raw_line in stdout.splitlines():
            parsed = self._parse_reflog_line(raw_line)
            if parsed is None:
                continue
            if skipping:
                if parsed["commit_hash"] == last_hash:
                    skipping = False
                # No cosechar la entrada que ya fue el cursor
                continue
            events.append({
                "type": "COMMIT_MADE",
                "timestamp": parsed["timestamp"],
                "commit_hash": parsed["commit_hash"],
                "commit": parsed["commit_hash"],  # alias pedido por la spec
                "message": parsed["message"],
            })
        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        last_commit = cursor.get("commit")
        if harvested_raw_events:
            last_commit = harvested_raw_events[-1].get("commit_hash", last_commit)
        new_cursor = dict(cursor)
        new_cursor["commit"] = last_commit or ""
        return new_cursor
