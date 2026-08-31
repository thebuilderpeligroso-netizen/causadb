"""
CausaDB Adapter Template (G.0).

Template para crear nuevos adapters que interactúan con CausaDB.
Este módulo importa las clases del núcleo (LedgerWriter, ReplayEngine, LedgerIndex)
y delega en ellas — no reimplementa lógica (Artículo II).

Uso:
    from causadb.adapters.template import log_event, query_events, get_state

    # Loggear un evento
    result = log_event("FILE_MODIFIED", {"path": "src/main.py", "action": "edit"},
                       ledger_path="/ruta/al/ledger.log")

    # Consultar eventos
    events = query_events({"event_type": "FILE_MODIFIED"})

    # Obtener estado completo
    state = get_state()
"""

import os
from typing import Any, Dict, List, Optional
from types import MappingProxyType

from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb._ledger_index import LedgerIndex
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Resolución del ledger
# ---------------------------------------------------------------------------

def _resolve_ledger(ledger_path: Optional[str] = None) -> str:
    """Resolver ruta del ledger: argumento explícito > env var > error.

    Args:
        ledger_path: Ruta explícita (puede ser relativa o absoluta).

    Returns:
        Ruta absoluta al archivo ledger.

    Raises:
        ValueError: Si no se puede resolver ninguna ruta.
    """
    if ledger_path is not None:
        return os.path.abspath(ledger_path)

    env_path = os.environ.get("CAUSADB_LEDGER_PATH")
    if env_path:
        return os.path.abspath(env_path)

    raise ValueError(
        "No ledger path provided. Pass `ledger_path` explicitly or "
        "set the CAUSADB_LEDGER_PATH environment variable."
    )


# ---------------------------------------------------------------------------
# API pública del template
# ---------------------------------------------------------------------------

def log_event(
    event_type: str,
    payload: Dict[str, Any],
    ledger_path: Optional[str] = None,
    ctx_id: str = "default",
    source: str = "adapter",
    source_type: str = "agent",
    parent_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Log an event to CausaDB via LedgerWriter.

    Construye un ``CanonicalEvent`` y lo persiste en el ledger usando
    ``LedgerWriter.append()``.

    Args:
        event_type: Tipo de evento (e.g. ``"FILE_MODIFIED"``, ``"COMMAND_RUN"``).
            Debe ser un valor válido de ``EventType`` o un tipo registrado.
        payload: Diccionario con los datos del evento.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.
        ctx_id: Context ID para agrupar eventos relacionados.
        source: Identificador del origen del evento.
        source_type: ``"human"``, ``"agent"`` o ``"llm"``.
        parent_event_id: ID del evento padre para encadenamiento causal.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger, la validación del
            evento, o la escritura.
    """
    ledger = _resolve_ledger(ledger_path)

    event = CanonicalEvent(
        event_type=EventType(event_type),
        ctx_id=ctx_id,
        source=source,
        source_type=source_type,
        payload=MappingProxyType(payload),
        parent_event_id=parent_event_id,
    )

    writer = LedgerWriter(ledger)
    writer.append(event)

    return {
        "event_id": event.event_id,
        "hash": writer.last_hash,
        "timestamp": event.timestamp,
    }


def query_events(
    filter_dict: Dict[str, Optional[str]],
    ledger_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query events from CausaDB via LedgerIndex.

    Delega en ``LedgerIndex.query()`` con filtros AND-combinados.

    Args:
        filter_dict: Diccionario con claves opcionales:
            ``event_type``, ``ctx_id``, ``parent_event_id``, ``source``.
            Los valores ``None`` se ignoran.
        ledger_path: Ruta absoluta al archivo ledger.

    Returns:
        Lista de entradas de ledger (evento + hash + prev_hash) que
        coinciden con todos los filtros, ordenadas por sequence_number.
    """
    ledger = _resolve_ledger(ledger_path)
    index = LedgerIndex(ledger)

    results = index.query(
        event_type=filter_dict.get("event_type"),
        ctx_id=filter_dict.get("ctx_id"),
        parent_event_id=filter_dict.get("parent_event_id"),
        source=filter_dict.get("source"),
    )
    return results


def get_state(
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Get full causal state from CausaDB via ReplayEngine.

    Reconstruye el estado completo del ledger usando
    ``ReplayEngine.reconstruct_state()``, que valida la hash chain
    antes de aplicar todos los eventos.

    Args:
        ledger_path: Ruta absoluta al archivo ledger.

    Returns:
        Diccionario con el estado reconstruido (archivos modificados,
        comandos ejecutados, sesiones, decisiones, etc.).

    Raises:
        ReplayIntegrityError: Si la hash chain del ledger está corrupta.
    """
    ledger = _resolve_ledger(ledger_path)
    engine = ReplayEngine(ledger)
    return engine.reconstruct_state()
