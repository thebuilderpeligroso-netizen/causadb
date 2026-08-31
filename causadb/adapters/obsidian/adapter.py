"""
Obsidian Plugin Adapter (G.2).

Adapter for Obsidian (https://obsidian.md) community plugins.
Logs note changes and queries CausaDB by note path.

Delega en ``causadb.adapters.template`` (Artículo II — no reimplementar lógica
del núcleo).

Uso:
    from causadb.adapters.obsidian.adapter import log_note_change, query_notes_by_path

    # Loggear un cambio de nota
    result = log_note_change("vault/ideas.md", "Ideas",
                             ledger_path="/ruta/al/ledger.log")

    # Consultar cambios por path
    events = query_notes_by_path("vault/ideas.md",
                                 ledger_path="/ruta/al/ledger.log")
"""

from typing import Any, Dict, List, Optional

from causadb.adapters import template


# ---------------------------------------------------------------------------
# API pública del adapter Obsidian
# ---------------------------------------------------------------------------

def log_note_change(
    note_path: str,
    note_title: str,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a note change event to CausaDB.

    Crea un evento ``FILE_MODIFIED`` con payload ``{"path", "title", "action"}``
    y lo persiste delegando en ``template.log_event()``.

    El campo ``source`` se fija en ``"obsidian"`` para identificar el origen
    del evento en el ledger.

    Args:
        note_path: Path de la nota dentro del vault de Obsidian
            (e.g. ``"vault/ideas.md"``).
        note_title: Título visible de la nota
            (e.g. ``"Ideas"``).
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger, la validación del
            evento, o la escritura.
    """
    payload: Dict[str, Any] = {
        "path": note_path,
        "title": note_title,
        "action": "edit",
    }
    return template.log_event(
        event_type="FILE_MODIFIED",
        payload=payload,
        ledger_path=ledger_path,
        source="obsidian",
    )


def query_notes_by_path(
    note_path: str,
    ledger_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query note change events by note path.

    Delega en ``template.query_events()`` para obtener todos los eventos
    ``FILE_MODIFIED``, y luego filtra aquellos cuyo ``payload.path`` contenga
    el texto de ``note_path`` (substring match).

    Args:
        note_path: Path (o substring) para filtrar notas
            (e.g. ``"vault/ideas.md"``).
        ledger_path: Ruta absoluta al archivo ledger.

    Returns:
        Lista de entradas de ledger (evento + hash + prev_hash) que
        coinciden con el filtro de path.
    """
    results = template.query_events(
        {"event_type": "FILE_MODIFIED"},
        ledger_path=ledger_path,
    )
    return [
        entry
        for entry in results
        if note_path
        in entry.get("event", {}).get("payload", {}).get("path", "")
    ]
