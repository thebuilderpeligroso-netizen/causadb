# CausaDB Jupyter Adapter — G.3

Adapter para Jupyter que loggea ejecuciones de celdas y carga de datasets
al ledger de CausaDB.

## Instalación

```bash
pip install causadb[jupyter]
```

O desde el repositorio local:

```bash
pip install -e ".[jupyter]"
```

## Magic Command `%%causadb`

El adapter expone un magic command `%%causadb` que loggea la celda ejecutada
como un evento `COMMAND_RUN` en el ledger de CausaDB.

```python
%%causadb
import pandas as pd

df = pd.read_csv("data.csv")
print(df.head())
```

Esto registra un evento con:
- **event_type:** `COMMAND_RUN`
- **payload:** `{"cell": "import pandas as pd\n\ndf = pd.read_csv(...", "output_truncated": false}`

## Eventos

| Origen | EventType | Payload |
|--------|-----------|---------|
| Cell execution | `COMMAND_RUN` | `{"cell": "<code[:500]>", "output_truncated": true/false}` |
| Dataframe load | `DATA_LOADED` | `{"source": "data.csv", "rows": 1500, "columns": 10}` |

## Configuración

El ledger path se configura via la variable de entorno `CAUSADB_LEDGER_PATH`:

```bash
export CAUSADB_LEDGER_PATH=/ruta/al/ledger.log
```

O se pasa explícitamente como argumento `ledger_path` a cada función.

## API Pública

### `log_cell_execution(cell_code, output, ledger_path=None)`

Loggea la ejecución de una celda Jupyter como evento `COMMAND_RUN`.

- `cell_code` (str): Código de la celda ejecutada (truncado a 500 chars).
- `output` (str): Output de la celda.
- `ledger_path` (str | None): Ruta al ledger. Si se omite, se lee de `CAUSADB_LEDGER_PATH`.
- Returns: `dict` con `event_id`, `hash`, `timestamp`.

### `log_dataframe_load(source, rows, columns, ledger_path=None)`

Loggea la carga de un dataset como evento `DATA_LOADED`.

- `source` (str): Fuente del dataset (path, URL, etc.).
- `rows` (int): Número de filas.
- `columns` (int): Número de columnas.
- `ledger_path` (str | None): Ruta al ledger.
- Returns: `dict` con `event_id`, `hash`, `timestamp`.

## Tests

```bash
cd causadb && source .venv/bin/activate && python -m pytest tests/test_adapter_jupyter.py -v
```
