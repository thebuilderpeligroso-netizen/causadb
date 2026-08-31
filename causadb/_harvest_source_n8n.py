"""HarvestSource — puntita n8n (Fase 15.4).

Lee las ejecuciones de n8n desde su store SQLite real
(``~/.n8n/database.sqlite``) y las convierte en eventos canónicos.

Schema real (verificado sobre el db real):

  - ``execution_entity`` (id INTEGER PRIMARY KEY AUTOINCREMENT, workflowId,
    finished, mode, startedAt, stoppedAt, status, ...)
  - ``execution_data`` (executionId INT, data TEXT [JSON], workflowData TEXT [JSON])
  - ``workflow_entity`` (id varchar, name varchar, active, ...)

Mapeo de la puntita (una fila ``execution_entity`` → uno o dos raw dicts):
  - Siempre: COMMAND_RUN con command = "n8n:run:<workflow_name>"
  - Si status = "error": evento adicional OBSERVATION (severity="blocker")
  - Los nodos ejecutados de ``execution_data.data.resultData.runData`` se
    agregan como ``nodes_executed`` al COMMAND_RUN (metadata adicional).

Cursor: ``{"max_execution_id": int}`` — barrido secuencial por
``execution_entity.id`` (autoincrement). Solo avanza sobre eventos
efectivamente escritos (atomicidad, Artículo I).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — read-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from causadb._harvest_source import HarvestSource


def _derive_default_db_path() -> str:
    """Store de n8n: env override o el path por defecto del usuario."""
    env_path = os.environ.get("CAUSADB_N8N_DB_PATH")
    if env_path:
        return env_path
    return os.path.join(os.path.expanduser("~"), ".n8n", "database.sqlite")


def _parse_execution_data(data_json: str) -> dict:
    """Parse el JSON de execution_data.data.

    n8n usa un formato de array comprimido donde los valores se
    serializan como un array de strings con referencias numéricas.
    Para el harvest, solo necesitamos ``resultData.runData`` y el
    mensaje de error si existe.
    """
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(data, dict):
        return {}

    return data


def _extract_error_message(data: dict) -> Optional[str]:
    """Extrae el mensaje de error de execution_data.data.

    En el formato comprimido de n8n, el error está en posiciones
    específicas del array. Buscamos el mensaje de error en el dict
    de nivel superior o en resultData.error.
    """
    # Formato directo (no comprimido): data.error.message
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("message")
        if isinstance(error, str):
            return error
        # resultData.error
        rd = data.get("resultData")
        if isinstance(rd, dict):
            rderr = rd.get("error")
            if isinstance(rderr, dict):
                return rderr.get("message")
            if isinstance(rderr, str):
                return rderr
    return None


def _extract_nodes(data: dict) -> list[str]:
    """Extrae los nombres de nodos ejecutados de resultData.runData."""
    rd = data.get("resultData")
    if not isinstance(rd, dict):
        return []
    run_data = rd.get("runData")
    if not isinstance(run_data, dict):
        return []
    return sorted(run_data.keys())


def _execution_to_raws(
    exec_row: tuple,
    data_dict: dict,
    workflow_name: str,
) -> list[dict]:
    """Mapea UNA fila execution_entity (con su execution_data.data ya
    parseado) a una lista de raw dicts canónicos.

    Retorna 1 raw dict (COMMAND_RUN) para ejecuciones normales, o 2
    (COMMAND_RUN + OBSERVATION) para ejecuciones con error.
    """
    # Campos de execution_entity (orden del SELECT en harvest())
    exec_id, workflow_id, finished, mode, started_at, stopped_at, status = exec_row

    nodes = _extract_nodes(data_dict)
    error_msg = _extract_error_message(data_dict)

    timestamp = started_at or stopped_at or ""
    # Normalizar timestamp SQL a ISO (el harvester lo normaliza después)
    if timestamp and "T" not in str(timestamp):
        timestamp = str(timestamp).replace(" ", "T")

    command_raw = {
        "type": "COMMAND_RUN",
        "timestamp": timestamp,
        "command": f"n8n:run:{workflow_name}",
        "execution_id": exec_id,
        "workflow_id": workflow_id,
        "mode": mode,
        "status": status,
        "finished": bool(finished),
        "nodes": nodes,
    }
    if stopped_at:
        command_raw["stopped_at"] = str(stopped_at).replace(" ", "T")

    raws = [command_raw]

    # Si hay error, agregar OBSERVATION
    if status == "error" and error_msg:
        raws.append({
            "type": "OBSERVATION",
            "timestamp": timestamp,
            "file_path": f"n8n:execution:{exec_id}",
            "line_number": 0,
            "description": error_msg,
            "severity": "blocker",
        })

    return raws


class N8nHarvestSource(HarvestSource):
    """Fuente de harvest para las ejecuciones de n8n.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de n8n. Default:
            ``CAUSADB_N8N_DB_PATH`` o ``~/.n8n/database.sqlite``
            (override para tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        return "n8n"

    def cursor_key(self) -> str:
        return "harvest.n8n"

    def detect(self) -> bool:
        return os.path.isfile(self.db_path)

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        cursor = cursor or {}
        max_exec_id = int(cursor.get("max_execution_id", 0))
        raws: list[dict] = []

        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            # Cargar workflow names en memoria (pocos workflows)
            wf_map = {}
            for wf_id, wf_name in con.execute(
                "SELECT id, name FROM workflow_entity"
            ).fetchall():
                wf_map[wf_id] = wf_name

            query = (
                "SELECT e.id, e.workflowId, e.finished, e.mode, "
                "e.startedAt, e.stoppedAt, e.status "
                "FROM execution_entity e "
                "WHERE e.id > ? "
                "ORDER BY e.id"
            )
            rows = con.execute(query, (max_exec_id,)).fetchall()

            for row in rows:
                exec_id = row[0]
                workflow_id = row[1]
                workflow_name = wf_map.get(workflow_id, workflow_id)

                # Cargar execution_data
                data_row = con.execute(
                    "SELECT data FROM execution_data WHERE executionId = ?",
                    (exec_id,),
                ).fetchone()

                data_dict = {}
                if data_row and data_row[0]:
                    data_dict = _parse_execution_data(data_row[0])

                event_raws = _execution_to_raws(row, data_dict, workflow_name)
                for raw in event_raws:
                    raw["__harvest_id"] = exec_id
                    raw["__harvest_workflow_name"] = workflow_name
                    raws.append(raw)

        finally:
            con.close()

        return raws

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        new_max = int(cursor.get("max_execution_id", 0))
        for ev in harvested_raw_events:
            hid = ev.get("__harvest_id")
            if hid is not None:
                new_max = max(new_max, int(hid))
        return {"max_execution_id": new_max}