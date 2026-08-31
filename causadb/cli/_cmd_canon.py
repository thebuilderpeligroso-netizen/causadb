"""`causadb canon` — imprime la guía del agente (docs/canon.md).

Doctrina (BIT-49 / briefing:92): el canon viaja DENTRO del producto y se
mantiene una sola vez. El archivo de reglas de cada tool lleva solo un
puntero a este comando (o al resource MCP causadb://canon) — nunca una URL
externa ni una ruta absoluta de máquina. Este comando resuelve el canon
relativo al paquete instalado, así funciona en cualquier máquina.
"""
import json
import sys
from pathlib import Path
from typing import Tuple, Optional


def _resolve_canon_path() -> Optional[str]:
    """Resuelve docs/canon.md relativo al paquete instalado.

    Busca en orden:
    1. ``causadb/docs/canon.md`` — canon empaquetado junto al paquete.
    2. ``docs/canon.md`` (repo root) — dev/editable install.
    Nunca usa rutas del home del desarrollador.
    """
    import causadb
    pkg_root = Path(causadb.__file__).resolve().parent
    candidates = [
        pkg_root / "docs" / "canon.md",
        pkg_root.parent / "docs" / "canon.md",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def cmd_canon(args) -> Tuple[int, str]:
    """Imprime el contenido de docs/canon.md.

    Devuelve (0, markdown) con el canon; (1, JSON error) si no existe.
    """
    canon_path = _resolve_canon_path()
    if canon_path is None:
        return (1, json.dumps({
            "error": "Canon (docs/canon.md) no encontrado en el paquete causadb.",
            "message": "Reinstalar el paquete o proveer la guía en causadb/docs/canon.md.",
        }))
    try:
        text = Path(canon_path).read_text(encoding="utf-8")
        return (0, text)
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
