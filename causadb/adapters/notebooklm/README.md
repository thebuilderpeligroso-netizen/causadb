# CausaDB → NotebookLM / Gemini Adapter — G.1

Adapter liviano para que **NotebookLM** o **Gemini** (Google AI Studio)
puedan consultar el ledger de CausaDB via la tool `causadb_query`.

## Instalación

No requiere dependencias adicionales. El adapter usa solo stdlib +
las clases del núcleo de CausaDB (ya instaladas).

```bash
pip install causadb
```

## Configuración de la tool `causadb_query` en Gemini

### 1. Definir la función/tool en el manifest de Gemini

En Google AI Studio o via API, registrá una **function declaration**
con el siguiente schema:

```json
{
  "name": "causadb_query",
  "description": "Consulta eventos del ledger causal de CausaDB. Filtros opcionales combinados con AND.",
  "parameters": {
    "type": "object",
    "properties": {
      "event_type": {
        "type": "string",
        "description": "Filtrar por tipo de evento (e.g. FILE_MODIFIED, COMMAND_RUN, DECISION_LOG)"
      },
      "ctx_id": {
        "type": "string",
        "description": "Filtrar por context ID"
      },
      "source": {
        "type": "string",
        "description": "Filtrar por origen del evento"
      },
      "parent_event_id": {
        "type": "string",
        "description": "Filtrar por ID del evento padre"
      }
    }
  }
}
```

### 2. Implementar el handler del backend

El endpoint HTTP que Gemini va a llamar debe:

1. Parsear los filtros del body de la request.
2. Llamar a `adapter.query(query_params, ledger_path=ledger_path)`.
3. Pasar el resultado por `adapter.format_for_notebooklm(events)`.
4. Devolver el markdown como respuesta.

Ejemplo de handler con Flask:

```python
import os
from flask import Flask, request, jsonify
from causadb.adapters.notebooklm.adapter import query, format_for_notebooklm

app = Flask(__name__)

LEDGER_PATH = os.environ.get("CAUSADB_LEDGER_PATH", "/data/ledger.log")

@app.route("/api/query", methods=["POST"])
def handle_query():
    params = request.get_json(silent=True) or {}
    events = query(params, ledger_path=LEDGER_PATH)
    markdown = format_for_notebooklm(events)
    return jsonify({"result": markdown})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7457)
```

### 3. Ledger path

El ledger path se resuelve en este orden:

1. Argumento `ledger_path` explícito en `query()`.
2. Variable de entorno `CAUSADB_LEDGER_PATH`.
3. Error `ValueError` si no se encuentra ninguna.

```bash
export CAUSADB_LEDGER_PATH=/ruta/al/ledger.log
```

## Uso desde Python

```python
from causadb.adapters.notebooklm.adapter import query, format_for_notebooklm

# Consultar eventos de tipo FILE_MODIFIED
events = query({"event_type": "FILE_MODIFIED"}, ledger_path="/ruta/ledger.log")

# Formatear para NotebookLM
markdown = format_for_notebooklm(events)
print(markdown)
```

## Tests

```bash
cd /ruta/del/proyecto
source .venv/bin/activate
python -m pytest tests/test_adapter_notebooklm.py -v
```

## API pública

| Función | Descripción |
|---------|-------------|
| `query(query_params, ledger_path=None)` | Consulta eventos. Delega en `template.query_events()`. |
| `format_for_notebooklm(events)` | Formatea eventos como markdown con event_id, type, timestamp y payload snippet. |
