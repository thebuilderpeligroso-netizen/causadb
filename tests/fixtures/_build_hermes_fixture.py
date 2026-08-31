"""Genera el fixture de Hermes Agent: ``tests/fixtures/hermes_fixture.db``.

Copia PEQUEÑA y fiel de un ``state.db`` REAL de Hermes Agent v0.19.1
(Artículo IX — datos reales, no mocks), generado el 2026-08-02 ejecutando
Hermes contra Ollama local (``qwen3.5:4b``, ``llama3.1:8b``) en
``/tmp/opencode/hermes-home/state.db``.

El DDL replica el ``SCHEMA_SQL`` de ``hermes_state_common.py`` del repo
NousResearch/hermes-agent (verificado contra el código oficial, no spec de
memoria). Los campos largos (system_prompt, reasoning, tool result) se
recortan con el marcador ``[recortado-fixture]``, igual que las fixtures
opencode/gemini.

Las dos sesiones extraídas ejercitan ambos mapeos de la puntita:
- ``20260802_101617_82f322`` (qwen3.5:4b): user + assistant CON
  ``reasoning_content`` poblado → REASONING_STEP/thoughts + LLM_INVOKED.
- ``20260802_102154_c35163`` (llama3.1:8b): ciclo COMPLETO de tool call →
  user → assistant con ``tool_calls`` (JSON array) → ``role='tool'`` con
  ``tool_call_id`` matcheando ``tool_calls[].id`` → assistant final.
  Ejercita el pairing tool↔result y TOOL_CALLED.

Hallazgos reales del store que el plan contempla:
- ``messages.token_count`` es NULL (los tokens viven en ``sessions``/
  ``session_model_usage``), no por-mensaje.
- ``sessions.model`` guarda el string con prefijo provider (``local:...``,
  ``custom:...``) o desnudo — la puntita normaliza.
- ``reasoning`` y ``reasoning_content`` se escriben idénticos.
- ``timestamp`` es REAL epoch (segundos) → ``_s_to_iso`` (la puntita usa
  segundos, no milisegundos como OpenJarvis).

Re-ejecutar para regenerar: ``python tests/fixtures/_build_hermes_fixture.py``
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "hermes_fixture.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT
);

CREATE TABLE IF NOT EXISTS session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
"""

SESSION_ROWS = [
    {
        "id": "20260802_101617_82f322",
        "source": "cli",
        "user_id": None,
        "model": "qwen3.5:4b",
        "model_config": '{"max_iterations": 90, "reasoning_config": null, "max_tokens": null}',
        "system_prompt": None,
        "started_at": 1785676577.8264675,
        "ended_at": 1785676625.819248,
        "end_reason": "agent_close",
        "message_count": 2,
        "tool_call_count": 0,
        "input_tokens": 2050,
        "output_tokens": 1339,
        "cwd": "/tmp/opencode/hermes-agent",
        "billing_provider": "custom",
        "billing_base_url": "http://127.0.0.1:11434/v1",
        "estimated_cost_usd": 0.0,
        "cost_status": "unknown",
        "api_call_count": 1,
    },
    {
        "id": "20260802_102154_c35163",
        "source": "cli",
        "user_id": None,
        "model": "llama3.1:8b",
        "model_config": '{"max_iterations": 90, "reasoning_config": null, "max_tokens": null}',
        "system_prompt": None,
        "started_at": 1785676914.6404264,
        "ended_at": 1785676932.681899,
        "end_reason": "agent_close",
        "message_count": 4,
        "tool_call_count": 1,
        "input_tokens": 4100,
        "output_tokens": 215,
        "cwd": "/tmp/opencode/hermes-agent",
        "billing_provider": "custom",
        "billing_base_url": "http://127.0.0.1:11434/v1",
        "estimated_cost_usd": 0.0,
        "cost_status": "unknown",
        "api_call_count": 2,
    },
]

MESSAGE_ROWS = [
    {
        "id": 5,
        "session_id": "20260802_101617_82f322",
        "role": "user",
        "content": "Escribe una frase corta saludando en español.",
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676577.8282998,
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
    {
        "id": 6,
        "session_id": "20260802_101617_82f322",
        "role": "assistant",
        "content": "¡Hola! Espero que estés teniendo un gran día.",
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676625.817056,
        "finish_reason": "stop",
        "reasoning": "Thinking Process:\n\n1.  **Analyze the Request:**\n    *   Input: \"Escribe una frase corta "
        "saludando en español.\"\n    *   Task: Generate a brief greeting sente\n"
        "…[recortado-fixture]",
        "reasoning_content": "Thinking Process:\n\n1.  **Analyze the Request:**\n    *   Input: \"Escribe una frase "
        "corta saludando en español.\"\n    *   Task: Generate a brief greeting sente\n"
        "…[recortado-fixture]",
        "reasoning_details": None,
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
    {
        "id": 19,
        "session_id": "20260802_102154_c35163",
        "role": "user",
        "content": "Lista los archivos...",
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676914.642556,
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
    {
        "id": 20,
        "session_id": "20260802_102154_c35163",
        "role": "assistant",
        "content": "",
        "tool_calls": (
            '[{"id": "call_su9umhks", "call_id": "call_su9umhks", '
            '"response_item_id": "fc_su9umhks", "type": "function", '
            '"function": {"name": "terminal", "arguments": "{\\"command\\":\\"ls -1 '
            '/tmp/opencode\\",\\"background\\":\\"false\\"}"}}]'
        ),
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676930.3857608,
        "finish_reason": "tool_calls",
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
    {
        "id": 21,
        "session_id": "20260802_102154_c35163",
        "role": "tool",
        "content": '{"output": "hermes-agent\\nhermes-home\\nhermes-venv", "exit_code": 0, "error": null}',
        "tool_call_id": "call_su9umhks",
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676930.4714081,
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
    {
        "id": 22,
        "session_id": "20260802_102154_c35163",
        "role": "assistant",
        "content": "Para listar los archivos...",
        "effect_disposition": None,
        "token_count": None,
        "timestamp": 1785676932.6770346,
        "finish_reason": "stop",
        "observed": 0,
        "active": 1,
        "compacted": 0,
        "platform_message_id": None,
    },
]


def _build():
    if os.path.exists(OUT):
        os.remove(OUT)
    con = sqlite3.connect(OUT)
    con.executescript(SCHEMA_SQL)
    for s in SESSION_ROWS:
        cols = [c for c in s.keys() if s[c] is not None]
        con.execute(
            "INSERT INTO sessions (" + ", ".join(cols) + ") VALUES ("
            + ", ".join("?" * len(cols)) + ")",
            [s[c] for c in cols],
        )
    for m in MESSAGE_ROWS:
        cols = [c for c in m.keys() if m[c] is not None]
        con.execute(
            "INSERT INTO messages (" + ", ".join(cols) + ") VALUES ("
            + ", ".join("?" * len(cols)) + ")",
            [m[c] for c in cols],
        )
    con.commit()
    con.close()
    ro = sqlite3.connect("file:" + OUT + "?mode=ro", uri=True)
    n_messages = ro.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    max_rowid = ro.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
    n_tool = ro.execute(
        "SELECT COUNT(*) FROM messages WHERE role='tool' OR tool_calls IS NOT NULL"
    ).fetchone()[0]
    ro.close()
    print("fixture OK: %s (%d messages, max_rowid=%d, tool_rows=%d)" % (OUT, n_messages, max_rowid, n_tool))


if __name__ == "__main__":
    _build()
