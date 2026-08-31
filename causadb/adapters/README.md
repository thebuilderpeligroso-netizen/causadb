# Guía de Adaptadores CausaDB — G.0

## ¿Qué es un adapter?

Un **adapter** es un módulo Python dentro de `causadb/adapters/` que expone una
interfaz simplificada para que un agente, herramienta externa o script
interactúe con CausaDB. Los adapters **no reimplementan lógica** — delegan en
las clases del núcleo (`LedgerWriter`, `ReplayEngine`, `LedgerIndex`, etc.)
siguiendo el Artículo II de la Constitución.

Un adapter típicamente provee 3 operaciones fundamentales:

| Operación | Función del template | Delega en |
|-----------|---------------------|-----------|
| Escribir  | `log_event()` | `LedgerWriter.append()` |
| Consultar | `query_events()` | `LedgerIndex.query()` |
| Reconstruir | `get_state()` | `ReplayEngine.reconstruct_state()` |

---

## 5 Pasos para crear un adapter nuevo

### Paso 1 — Crear carpeta en `adapters/`

Crea un subdirectorio con el nombre de tu adapter dentro de
`causadb/causadb/adapters/`:

```bash
mkdir causadb/causadb/adapters/mi_adapter/
touch causadb/causadb/adapters/mi_adapter/__init__.py
```

### Paso 2 — Importar funciones del template

En tu `__init__.py` (o en el módulo principal), importá las funciones base
desde `template.py`:

```python
from causadb.adapters.template import log_event, query_events, get_state
from causadb.adapters.template import _resolve_ledger  # si necesitás resolver rutas
```

### Paso 3 — Implementar `log_event()`, `query_events()`, `get_state()`

Tu adapter debe exponer estas 3 funciones. **No reimplementes la lógica** —
delegá al template o directamente a las clases del núcleo.

Ejemplo mínimo para un adapter de monitoreo:

```python
# causadb/adapters/mi_adapter/__init__.py

from typing import Any, Dict, List, Optional
from causadb.adapters.template import log_event, query_events, get_state

def notify_file_change(path: str, ledger_path: Optional[str] = None) -> dict:
    """Loggea un cambio de archivo y devuelve el resultado."""
    return log_event(
        event_type="FILE_MODIFIED",
        payload={"path": path, "action": "modified"},
        ctx_id="monitor",
        source="mi_adapter",
        ledger_path=ledger_path,
    )

def get_recent_commands(n: int = 10, ledger_path: Optional[str] = None) -> list:
    """Obtiene los últimos N comandos ejecutados."""
    events = query_events(
        {"event_type": "COMMAND_RUN"},
        ledger_path=ledger_path,
    )
    return events[-n:]

def get_full_snapshot(ledger_path: Optional[str] = None) -> dict:
    """Obtiene el snapshot completo del ledger."""
    return get_state(ledger_path=ledger_path)
```

### Paso 4 — Escribir tests

Cada adapter debe tener al menos 2 tests:

1. **Test funcional** — Verifica que `log_event()` escribe en el ledger y que
   `query_events()` / `get_state()` pueden leerlo.
2. **Test anti-teatro** — Verifica que el adapter falla correctamente cuando
   se le pasa un ledger_path inválido o un event_type inexistente (no puede
   pasar trivialmente con un mock que devuelva siempre OK).

Ejemplo:

```python
# tests/test_mi_adapter.py

import os
import tempfile
import pytest
from causadb._init import causadb_init
from causadb.adapters.mi_adapter import notify_file_change, get_recent_commands


class TestMiAdapter:
    @pytest.fixture
    def ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = causadb_init(os.path.join(tmp, "test_ws"))
            yield result["ledger_path"]

    def test_log_and_query(self, ledger):
        # Funcional: loggea un evento, después lo consulta
        result = notify_file_change("/tmp/test.txt", ledger_path=ledger)
        assert "event_id" in result
        assert "hash" in result

        cmds = get_recent_commands(ledger_path=ledger)
        # El único evento FILE_MODIFIED no debería aparecer en COMMAND_RUN
        assert len(cmds) == 0

    def test_invalid_ledger_raises(self):
        # Anti-teatro: ledger que no existe debe fallar
        with pytest.raises((ValueError, FileNotFoundError)):
            notify_file_change("/tmp/test.txt",
                               ledger_path="/no/existe/ledger.log")

    def test_invalid_event_type_raises(self):
        # Anti-teatro: event_type inválido debe fallar
        from causadb.adapters.template import log_event
        with pytest.raises(ValueError):
            log_event("NOT_A_REAL_TYPE", {}, ledger_path="/tmp/fake.log")
```

### Paso 5 — Documentar en el README del adapter

Cada adapter debe tener su propio `README.md` documentando:

- Qué hace el adapter
- Funciones públicas (firma, args, returns, raises)
- Ejemplo de uso
- Dependencias adicionales (si las tiene)
- Tests y cómo ejecutarlos

---

## Cómo usar la REST API de CausaDB

CausaDB expone una REST API liviana (stdlib `http.server`, sin dependencias
externas) en el puerto **7457** por defecto.

### Endpoints

| Método | Path | Descripción |
|--------|------|-------------|
| `GET` | `/api/health` | Health check del servidor |
| `POST` | `/api/log` | Registrar un evento en el ledger |
| `POST` | `/api/replay` | Reconstruir estado completo del ledger |
| `POST` | `/api/query` | Consultar eventos con filtros |

### Autenticación (`--ledger`)

La REST API no tiene autenticación propia. El ledger se asocia al servidor
al iniciarlo:

```bash
causadb serve --ledger /ruta/al/ledger.log --port 7457
```

O desde código:

```python
from causadb._rest_api import serve_in_thread

server = serve_in_thread("/ruta/al/ledger.log", port=7457)
# El server corre en un daemon thread. Hacé tus requests HTTP.
```

### Ejemplos con `curl`

```bash
# Health check
curl http://127.0.0.1:7457/api/health

# Loggear un evento
curl -X POST http://127.0.0.1:7457/api/log \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "FILE_MODIFIED",
    "ctx_id": "demo",
    "source": "curl",
    "payload": {"path": "README.md", "action": "edit"}
  }'

# Reconstruir estado
curl -X POST http://127.0.0.1:7457/api/replay \
  -H "Content-Type: application/json" \
  -d '{}'

# Consultar eventos por tipo
curl -X POST http://127.0.0.1:7457/api/query \
  -H "Content-Type: application/json" \
  -d '{"event_type": "FILE_MODIFIED"}'
```

### Schema del body de `/api/log`

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `event_type` | string | sí | Tipo de evento (`FILE_MODIFIED`, `COMMAND_RUN`, etc.) |
| `ctx_id` | string | sí | Context ID para agrupar |
| `source` | string | sí | Origen del evento |
| `source_type` | string | no | `"human"`, `"agent"` o `"llm"` (default: `"agent"`) |
| `payload` | object | no | Datos del evento (default: `{}`) |
| `parent_event_id` | string | no | ID del evento padre |
| `event_id` | string | no | UUID propio (si no se envía, se genera automáticamente) |
| `timestamp` | string | no | ISO 8601 (si no se envía, se genera automáticamente) |
| `metadata` | object | no | Metadatos opcionales (`trace_id`, `session_id`, etc.) |

---

## Cómo usar las MCP tools de CausaDB

CausaDB expone 15 herramientas MCP a través de `causadb-mcp`, que corre
sobre FastMCP con transporte stdio (compatible con OpenCode, Claude Desktop,
y cualquier host MCP).

### Iniciar el servidor MCP

```bash
# Auto-init: descubre o crea workspace en el CWD
causadb-mcp

# Sin auto-init: requiere ledger_path explícito en cada tool
causadb-mcp --no-auto-init
```

### Tools disponibles

| Tool | Descripción | Args clave |
|------|-------------|------------|
| `log` | Appendear un evento al ledger | `event_json` (JSON string), `ledger_path` |
| `replay` | Reconstruir estado completo | `ledger_path` |
| `query` | Consultar eventos con filtros | `event_type`, `ctx_id`, `parent_event_id`, `source`, `ledger_path` |
| `validate` | Validar hash chain | `ledger_path` |
| `sentinel` | Correr reglas de integridad | `ledger_path` |
| `sandbox` | Resumen de violaciones de sandbox | `ledger_path` |
| `feedback` | Listar HUMAN_FEEDBACK events | `ledger_path` |
| `stream` | Listar STREAM_INTERRUPTED events | `ledger_path` |
| `impact` | Cono causal downstream (blast radius) | `event_id`, `ledger_path` |
| `why` | Causal blame de una línea | `file_path`, `line_number`, `ledger_path` |
| `trace` | Cono causal upstream de una línea | `file_path`, `line_number`, `ledger_path` |
| `score` | Efficiency score (churn + waste + survival) | `ledger_path`, `session` |
| `skill_list` | Listar patrones aprendidos (skills) | `ledger_path`, `skill_types` |
| `log_decision` | Loggear decisión de governance | `reasoning`, `impact`, `decision_type`, `origin`, `ledger_path` |
| `revive` | Generar contexto de revival | `ledger_path`, `output_format`, `max_decisions` |

### Desde Python (vía MCP client)

```python
import subprocess
import json

# Iniciar causadb-mcp como subproceso
proc = subprocess.Popen(
    ["causadb-mcp"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)

# Enviar un tool call (formato JSON-RPC)
tool_call = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "log",
        "arguments": {
            "event_json": json.dumps({
                "event_type": "FILE_MODIFIED",
                "ctx_id": "demo",
                "source": "python-mcp",
                "payload": {"path": "test.txt", "action": "edit"},
            }),
        },
    },
}
proc.stdin.write(json.dumps(tool_call) + "\n")
proc.stdin.flush()
response = json.loads(proc.stdout.readline())
print(response)
```

### Configuración para OpenCode

CausaDB puede configurarse como servidor MCP en OpenCode. Usá:

```bash
causadb config mcp --tool opencode --project /ruta/al/proyecto
```

Esto genera un bloque JSON que se agrega a tu `opencode.jsonc`.

---

## Convenciones generales

- **Sin dependencias nuevas** — Los adapters usan solo stdlib + las clases
  del núcleo de CausaDB.
- **Error handling** — Todas las funciones deben propagar excepciones, no
  silenciarlas. Usar `raise` con mensajes descriptivos.
- **Type hints** — Todas las funciones públicas deben tener type hints
  completos (args y return).
- **Docstrings** — Formato Google-style (como en `template.py`).
- **Tests** — Mínimo 2 tests por adapter (1 funcional + 1 anti-teatro).
- **Hash chain** — Nunca escribir directo al `ledger.log`. Siempre usar
  `LedgerWriter.append()`.
