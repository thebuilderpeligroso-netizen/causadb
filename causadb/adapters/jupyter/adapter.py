"""
CausaDB Jupyter Adapter (G.3).

Loggea ejecuciones de celdas y carga de datasets al ledger de CausaDB.

Delega en ``causadb.adapters.template.log_event()`` para todas las
operaciones de escritura — no reimplementa lógica (Artículo II).

Tipos de eventos que genera:
    - COMMAND_RUN: Ejecución de una celda Jupyter.
    - DATA_LOADED: Carga de un dataset (registrado dinámicamente).

Uso:
    from causadb.adapters.jupyter.adapter import log_cell_execution, log_dataframe_load

    result = log_cell_execution("print('hello')", "hello",
                                ledger_path="/ruta/al/ledger.log")
    result = log_dataframe_load("data.csv", 1500, 10,
                                ledger_path="/ruta/al/ledger.log")
"""

from typing import Any, Dict, Optional

from causadb.adapters.template import log_event, _resolve_ledger
from causadb._event_registry import register_type, EventTypeSpec

# ---------------------------------------------------------------------------
# Registro dinámico de DATA_LOADED como tipo de evento custom
# ---------------------------------------------------------------------------
# DATA_LOADED no es un built-in de CausaDB. Se registra aquí para que
# EventType("DATA_LOADED") resuelva correctamente via _missing_().
# Los built-in types (COMMAND_RUN, etc.) ya están registrados en el core.
# ---------------------------------------------------------------------------
try:
    register_type(
        "DATA_LOADED",
        EventTypeSpec(required_fields={"source", "rows", "columns"}),
        builtin=False,
    )
except Exception:
    # Si ya está registrado (e.g. tests concurrentes), ignorar
    pass


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def log_cell_execution(
    cell_code: str,
    output: str,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Loggea la ejecución de una celda Jupyter.

    Registra un evento ``COMMAND_RUN`` con el código de la celda (truncado
    a 500 caracteres) y un flag indicando si el output fue truncado.

    Args:
        cell_code: Código de la celda ejecutada. Se trunca a 500 caracteres
            para evitar payloads excesivos.
        output: Output completo de la celda. No se almacena en el ledger;
            solo se registra si fue truncado via ``output_truncated``.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger, la validación del
            evento, o la escritura.
    """
    return log_event(
        event_type="COMMAND_RUN",
        payload={
            "cell": cell_code[:500],
            "output_truncated": len(output) > 100,
        },
        ctx_id="jupyter",
        source="jupyter",
        source_type="agent",
        ledger_path=ledger_path,
    )


def log_dataframe_load(
    source: str,
    rows: int,
    columns: int,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Loggea la carga de un dataset en Jupyter.

    Registra un evento ``DATA_LOADED`` con metadatos del dataset cargado.

    Args:
        source: Fuente del dataset (path de archivo, URL, etc.).
        rows: Número de filas del dataset.
        columns: Número de columnas del dataset.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger, la validación del
            evento, o la escritura.
    """
    return log_event(
        event_type="DATA_LOADED",
        payload={
            "source": source,
            "rows": rows,
            "columns": columns,
        },
        ctx_id="jupyter",
        source="jupyter",
        source_type="agent",
        ledger_path=ledger_path,
    )
