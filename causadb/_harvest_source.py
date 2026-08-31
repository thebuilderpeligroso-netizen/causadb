"""HarvestSource — clase base para fuentes de harvest (duck typing).

Sin ABC. Las subclases implementan los métodos requeridos; si falta
alguno, NotImplementedError se propaga naturalmente.
"""

import logging
import os
from typing import Iterable


def migrate_legacy_cursor(cursor: dict, chats_dirs: list[str]) -> dict:
    """Migra claves de cursor legacy (basename) a multi-store (slug/basename).

    GAP-01: antes del discovery multi-store, el cursor de gemini usaba el
    basename del archivo como clave (``session-*.jsonl``). Con varios stores
    el basename ya no identifica de forma única → las claves se re-encuadran
    a ``<slug>/<basename>``.

    Reglas (fail-safe, nunca pierde progreso):
      - clave legacy que existe en EXACTAMENTE un store → se re-encuadra a
        ``slug/basename`` preservando offset/mtime (sin duplicados).
      - colisión (existe en >1 store) → se preserva la clave vieja + warning
        (no se puede desambiguar; el store se re-cosecha, dedup por event_id).
      - ghost (no existe en ningún store) → se preserva la clave vieja
        (el archivo puede reaparecer; no se descarta progreso).

    Muta ``cursor`` in-place (el Harvester persiste el mismo dict) y lo
    retorna. Solo aplica en modo multi-store (el caller decide).
    """
    files = cursor.get("files")
    if not isinstance(files, dict):
        return cursor
    for key in list(files):
        if "/" in key:
            continue  # ya es multi-store
        matches = [
            d for d in chats_dirs
            if os.path.isfile(os.path.join(d, key))
        ]
        if len(matches) == 1:
            slug = os.path.basename(os.path.dirname(matches[0])) or "default"
            files[f"{slug}/{key}"] = files.pop(key)
        elif len(matches) > 1:
            logging.warning(
                "migrate_legacy_cursor: basename %r existe en %d stores; "
                "clave legacy preservada (no desambiguable)", key, len(matches)
            )
        else:
            logging.warning(
                "migrate_legacy_cursor: %r no existe en ningún store; "
                "clave legacy preservada (ghost)", key
            )
    return cursor


class HarvestSource:
    """Fuente de datos para el sedimenter (cosecha de eventos).

    Subclases DEBEN implementar (duck typing — sin herencia necesaria):

        source_type() -> str
            Identificador único, ej: "shell", "git", "jupyter".

        detect() -> bool
            Verifica si la fuente existe en el sistema actual.

        harvest(cursor: dict | None = None) -> Iterable[dict]
            Cosecha eventos desde la posición indicada por *cursor* hacia
            adelante. Retorna un iterable de dicts — lista o generador
            (duck typing; el harvester itera el resultado, no asume lista).
            Cada dict del resultado debe contener al menos ``type`` y
            ``timestamp``. El cursor es un dict opaco que la fuente usa
            para recordar su progreso (manejado externamente).

        cursor_key() -> str
            Clave única para almacenar/recuperar el cursor de esta fuente
            en el archivo de configuración del Harvester.
    """

    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path

    def source_type(self) -> str:
        raise NotImplementedError

    def detect(self) -> bool:
        raise NotImplementedError

    def harvest(self, cursor: dict | None = None) -> Iterable[dict]:
        raise NotImplementedError

    def cursor_key(self) -> str:
        raise NotImplementedError

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        """Avanza el cursor después de cosechar una tanda de eventos.

        El Harvester llama a este método después de que ``harvest()`` retornó
        y los eventos fueron escritos al ledger. La fuente decide qué guardar
        como cursor para la próxima cosecha.

        Por defecto usa un índice secuencial ``{"index": N}``. Las fuentes
        con cursores no-secuenciales (filesystem, obsidian, git, etc.) deben
        sobrescribir este método para preservar su formato de cursor.
        """
        old_index = cursor.get("index", 0) if cursor else 0
        return {"index": old_index + len(harvested_raw_events)}
