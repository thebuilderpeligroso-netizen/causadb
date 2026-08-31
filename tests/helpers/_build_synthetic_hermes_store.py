"""Helper sintético H2.0 — store Hermes de prueba para el contrato API_ATTEMPT.

Genera en tmp_path un store SQLite con schema v22 real (copia de la fixture
histórica, inmutable) + tabla ``session_model_usage`` poblada con 2 sesiones
(una con ``api_call_count=2``, la otra con 1). El store resultante ES
cosechable por ``HermesHarvestSource`` y no altera los conteos H1 (9/6).
NUNCA toca ``tests/fixtures/hermes_fixture.db``.
"""

import os
import shutil
import sqlite3

FIXTURE_DB = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "hermes_fixture.db"
)

USAGE_ROWS = [
    # (session_id, model, billing_provider, billing_base_url, billing_mode,
    #  task, api_call_count, input_tokens, output_tokens, cache_read_tokens,
    #  cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd,
    #  cost_status, cost_source, first_seen, last_seen)
    ("20260802_101617_82f322", "qwen3.5:4b", "custom",
     "http://127.0.0.1:11434/v1", "", "", 1, 2050, 1339, 0, 0, 0, 0.0, 0.0,
     "unknown", "none", 1785676577.8, 1785676625.8),
    ("20260802_102154_c35163", "llama3.1:8b", "custom",
     "http://127.0.0.1:11434/v1", "", "", 2, 4100, 215, 0, 0, 0, 0.0, 0.0,
     "unknown", "none", 1785676914.6, 1785676932.7),
]


def build_synthetic_hermes_store(db_path: str) -> str:
    """Copia la fixture (schema v22 + datos reales) y puebla session_model_usage.

    Devuelve la ruta del store. Lanza OSError si la fixture no existe.
    """
    if not os.path.exists(FIXTURE_DB):
        raise OSError(f"Fixture no encontrada: {FIXTURE_DB}")
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage ("
            " session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,"
            " model TEXT NOT NULL,"
            " billing_provider TEXT NOT NULL DEFAULT '',"
            " billing_base_url TEXT NOT NULL DEFAULT '',"
            " billing_mode TEXT NOT NULL DEFAULT '',"
            " task TEXT NOT NULL DEFAULT '',"
            " api_call_count INTEGER NOT NULL DEFAULT 0,"
            " input_tokens INTEGER NOT NULL DEFAULT 0,"
            " output_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
            " cache_write_tokens INTEGER NOT NULL DEFAULT 0,"
            " reasoning_tokens INTEGER NOT NULL DEFAULT 0,"
            " estimated_cost_usd REAL NOT NULL DEFAULT 0,"
            " actual_cost_usd REAL NOT NULL DEFAULT 0,"
            " cost_status TEXT,"
            " cost_source TEXT,"
            " first_seen REAL,"
            " last_seen REAL,"
            " PRIMARY KEY (session_id, model, billing_provider,"
            " billing_base_url, billing_mode, task))"
        )
        conn.executemany(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            USAGE_ROWS,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path