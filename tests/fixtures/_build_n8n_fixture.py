"""Genera el fixture de n8n: ``tests/fixtures/n8n_fixture.sqlite``.

Copia PEQUEÑA y fiel del store real ``~/.n8n/database.sqlite``
(Artículo IX — datos reales, no mocks). Solo incluye las ejecuciones
cuyo workflow tiene "CausaDB" en el nombre.

Para execution_data.data, extrae solo los campos que la puntita necesita
(``resultData.runData`` y ``error``) y los escribe como JSON válido.
Los campos largos se recortan con el marcador ``[recortado-fixture]``.

Ejecuciones incluidas:
  - exec 1: workflow "CausaDB Fixture with Webhook" (webhook, error)
  - exec 2: workflow "CausaDB Manual Trigger" (manual, success)

Re-ejecutar para regenerar: ``python tests/fixtures/_build_n8n_fixture.py``
"""

import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "n8n_fixture.sqlite")
REAL_DB = os.path.join(os.path.expanduser("~"), ".n8n", "database.sqlite")

MAX_STR_LEN = 500


def _truncate_str(s: str) -> str:
    """Trunca un string a MAX_STR_LEN chars con marcador."""
    if s is None:
        return None
    if len(s) <= MAX_STR_LEN:
        return s
    return s[:MAX_STR_LEN] + "…[recortado-fixture]"


def _extract_relevant_data(data_json: str) -> str:
    """Extrae solo los campos relevantes del execution_data.data y
    devuelve un JSON string válido (truncado si es necesario).

    La puntita solo necesita:
      - resultData.runData (nodos ejecutados)
      - error (mensaje de error si existe)
    """
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return "{}"

    # n8n usa un formato comprimido: array de strings con referencias
    # numéricas. Para el fixture, extraemos solo lo que necesitamos.
    if isinstance(data, list):
        # Formato comprimido de n8n: [meta, {}, runData, context, ...]
        # Pos 2: {"runData": "5", "lastNodeExecuted": "6", "error": "7"}
        # Pos 5: {"Webhook": "14"}  (runData keys → node names)
        # Pos 7: {"message": "15", "stack": "16"}  (error object)
        # Pos 15: "Unused Respond to Webhook..."  (error message)
        # Pos 16: stack trace string
        result = {}

        # Extraer nombres de nodos (pos 5 = dict con keys = node names)
        if len(data) > 5 and isinstance(data[5], dict):
            node_names = list(data[5].keys())
            result["resultData"] = {"runData": {n: [] for n in node_names}}

        # Extraer error (pos 7 = {"message": ref, "stack": ref})
        if len(data) > 7 and isinstance(data[7], dict):
            err = data[7]
            msg_ref = err.get("message")
            stack_ref = err.get("stack")
            # Resolver referencias (son índices en el array)
            msg = data[int(msg_ref)] if isinstance(msg_ref, str) and msg_ref.isdigit() and int(msg_ref) < len(data) else str(msg_ref)
            stack = data[int(stack_ref)] if isinstance(stack_ref, str) and stack_ref.isdigit() and int(stack_ref) < len(data) else str(stack_ref)
            result["error"] = {
                "message": _truncate_str(msg),
                "stack": _truncate_str(stack),
            }

        return json.dumps(result)

    # Formato directo (no comprimido)
    result = {}
    if isinstance(data, dict):
        rd = data.get("resultData")
        if isinstance(rd, dict):
            run_data = rd.get("runData")
            if isinstance(run_data, dict):
                result["resultData"] = {"runData": run_data}
        error = data.get("error")
        if error is not None:
            result["error"] = error

    if not result:
        return "{}"

    return json.dumps(result)


def _build():
    if not os.path.exists(REAL_DB):
        print(f"ERROR: Real DB not found at {REAL_DB}")
        return

    if os.path.exists(OUT):
        os.remove(OUT)

    src = sqlite3.connect(REAL_DB)
    dst = sqlite3.connect(OUT)

    # -- 1. Copiar schema de las 3 tablas necesarias --------------------------
    for table in ("execution_entity", "execution_data", "workflow_entity"):
        schema_sql = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if schema_sql is None:
            print(f"WARNING: table {table} not found in real DB")
            continue
        dst.execute(schema_sql[0])

    # -- 2. Encontrar workflows con "CausaDB" en el nombre -------------
    wf_rows = src.execute(
        "SELECT id, name, active FROM workflow_entity WHERE name LIKE '%CausaDB%'"
    ).fetchall()
    wf_ids = {r[0] for r in wf_rows}
    wf_names = {r[0]: r[1] for r in wf_rows}
    print(f"Found {len(wf_ids)} CausaDB workflows: {list(wf_names.values())}")

    # -- 3. Copiar workflow_entity rows ----------------------------------------
    for wf_id in wf_ids:
        row = src.execute(
            "SELECT * FROM workflow_entity WHERE id = ?", (wf_id,)
        ).fetchone()
        cols = [c[1] for c in src.execute("PRAGMA table_info(workflow_entity)")]
        placeholders = ", ".join("?" * len(cols))
        dst.execute(
            f"INSERT INTO workflow_entity ({', '.join(cols)}) VALUES ({placeholders})",
            row,
        )

    # -- 4. Encontrar ejecuciones de esos workflows ----------------------------
    exec_rows = src.execute(
        "SELECT * FROM execution_entity WHERE workflowId IN ({})".format(
            ",".join("?" * len(wf_ids))
        ),
        list(wf_ids),
    ).fetchall()
    exec_ids = {r[0] for r in exec_rows}
    print(f"Found {len(exec_ids)} executions: {sorted(exec_ids)}")

    # -- 5. Copiar execution_entity rows ---------------------------------------
    exec_cols = [c[1] for c in src.execute("PRAGMA table_info(execution_entity)")]
    for row in exec_rows:
        placeholders = ",".join("?" * len(exec_cols))
        dst.execute(
            f"INSERT INTO execution_entity ({', '.join(exec_cols)}) VALUES ({placeholders})",
            row,
        )

    # -- 6. Copiar execution_data rows (extrayendo JSON relevante) --------------
    data_cols = [c[1] for c in src.execute("PRAGMA table_info(execution_data)")]
    for eid in exec_ids:
        row = src.execute(
            "SELECT * FROM execution_data WHERE executionId = ?", (eid,)
        ).fetchone()
        if row is None:
            continue
        row_list = list(row)
        for i, col_name in enumerate(data_cols):
            if col_name in ("data", "workflowData") and row_list[i] is not None:
                row_list[i] = _extract_relevant_data(row_list[i])
        placeholders = ",".join("?" * len(data_cols))
        dst.execute(
            f"INSERT INTO execution_data ({', '.join(data_cols)}) VALUES ({placeholders})",
            row_list,
        )

    dst.commit()
    dst.close()
    src.close()

    # -- 7. Sanity check -------------------------------------------------------
    ro = sqlite3.connect("file:" + OUT + "?mode=ro", uri=True)
    n_exec = ro.execute("SELECT COUNT(*) FROM execution_entity").fetchone()[0]
    n_data = ro.execute("SELECT COUNT(*) FROM execution_data").fetchone()[0]
    n_wf = ro.execute("SELECT COUNT(*) FROM workflow_entity").fetchone()[0]
    # Verify JSON is valid
    for eid, data_json in ro.execute("SELECT executionId, data FROM execution_data"):
        try:
            d = json.loads(data_json)
            print(f"  exec {eid}: {json.dumps(d, indent=2)[:200]}")
        except json.JSONDecodeError as e:
            print(f"WARNING: invalid JSON in execution_data for exec {eid}: {e}")
    ro.close()
    print(f"fixture OK: {OUT} ({n_exec} executions, {n_data} data rows, {n_wf} workflows)")


if __name__ == "__main__":
    _build()