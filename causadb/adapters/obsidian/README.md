# Obsidian Plugin Adapter — G.2

Adapter para integrar CausaDB con Obsidian (https://obsidian.md) a través de
un plugin community.

## Arquitectura

```
┌──────────────┐     HTTP POST      ┌──────────────────┐
│   Obsidian   │ ──────────────────>│  CausaDB Daemon   │
│   (plugin)   │   /api/log         │  (localhost:7457) │
└──────────────┘                    └──────────────────┘
       │                                    │
       │  HTTP GET /api/query               │
       └────────────────────────────────────┘
```

El plugin de Obsidian envía eventos de modificación de notas al daemon de
CausaDB vía HTTP. El adapter del lado de CausaDB (este directorio) expone la
lógica que el daemon utiliza para procesar y consultar esos eventos.

---

## Configuración del Plugin Obsidian

1. Instalá (o desarrollá) un plugin community de Obsidian que soporte webhooks
   o llamadas HTTP a un endpoint configurable.

2. Configurá el plugin para que apunte a:

   ```
   URL base: http://localhost:7457
   ```

   (Este es el puerto por defecto del daemon de CausaDB.)

3. **Logging de cambios de nota** — el plugin debe enviar un POST a:

   ```
   POST /api/log
   ```

   Con body JSON:

   ```json
   {
     "event_type": "FILE_MODIFIED",
     "payload": {
       "path": "vault/mi_nota.md",
       "title": "Mi Nota",
       "action": "edit"
     },
     "source": "obsidian"
   }
   ```

   | Campo | Tipo | Descripción |
   |-------|------|-------------|
   | `event_type` | string | Siempre `"FILE_MODIFIED"` |
   | `payload.path` | string | Path relativo de la nota dentro del vault |
   | `payload.title` | string | Título visible de la nota |
   | `payload.action` | string | `"edit"`, `"create"`, `"delete"` |
   | `source` | string | Identificador del origen (`"obsidian"`) |

4. **Consulta de eventos por path de nota** — GET a:

   ```
   GET /api/query?q=<path>
   ```

   Donde `<path>` es el path (o substring) de la nota a consultar.

   Ejemplo:
   ```
   GET /api/query?q=vault/ideas.md
   ```

   Respuesta (JSON):
   ```json
   [
     {
       "event": {
         "event_id": "uuid-...",
         "event_type": "FILE_MODIFIED",
         "timestamp": "2026-07-29T12:00:00Z",
         "payload": {
           "path": "vault/ideas.md",
           "title": "Ideas",
           "action": "edit"
         }
       },
       "hash": "abc123...",
       "prev_hash": "GENESIS"
     }
   ]
   ```

---

## Uso desde Python (directo, sin daemon)

Si preferís llamar al adapter directamente desde Python (por ejemplo, desde un
script de automatización o un hook de Obsidian Local REST API):

```python
from causadb.adapters.obsidian.adapter import log_note_change, query_notes_by_path

# Loggear un cambio
result = log_note_change(
    "vault/ideas.md",
    "Ideas",
    ledger_path="/ruta/al/ledger.log",
)
print(f"Evento registrado: {result['event_id']}")

# Consultar por path
events = query_notes_by_path(
    "vault/ideas.md",
    ledger_path="/ruta/al/ledger.log",
)
for e in events:
    print(e["event"]["payload"]["title"])
```

---

## Requisitos

- CausaDB daemon corriendo en `localhost:7457` (o URL configurable)
- Obsidian v0.15+ (plugin community con soporte HTTP)
- Python 3.10+ si se usa el adapter directo

---

## Referencias

- [CausaDB Adapters README](../README.md) — guía general de adaptadores
- [CausaDB Daemon](../../_daemon.py) — implementación del daemon REST
- [Template adapter](../template.py) — funciones base delegadas (Artículo II)
