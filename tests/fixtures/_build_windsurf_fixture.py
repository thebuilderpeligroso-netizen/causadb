"""Genera el fixture de Windsurf/Devin: ``tests/fixtures/windsurf_sessions_fixture.sqlite``.

Copia PEQUEÑA y fiel del store real ``~/.local/share/devin/cli/sessions.db``
(Artículo IX — datos reales, no mocks). Los campos largos (content > 200 chars)
se recortan con el marcador ``[recortado-fixture]``, paths reales se sanitizan
a ``__FIXTURE_HOME__``.

La sesión real extraída es ``plume-grease`` (modelo ``swe-1-6-slow``):
120 message_nodes, 9 tool_call_state rows — un ciclo completo que ejercita
todos los mapeos de la puntita (LLM_INVOKED + TOOL_CALLED).

Re-ejecutar para regenerar: ``python tests/fixtures/_build_windsurf_fixture.py``
"""

import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "windsurf_sessions_fixture.db")
REAL_DB = os.path.join(
    os.path.expanduser("~"), ".local", "share", "devin", "cli", "sessions.db"
)

SESSION_ID = "plume-grease"
MAX_CONTENT_LEN = 200
HOME_PATH = os.path.expanduser("~")


def _sanitize_text(text: str) -> str:
    """Recorta contenido largo y sanitiza paths."""
    if not isinstance(text, str):
        return text
    # Sanitizar home path
    text = text.replace(HOME_PATH, "__FIXTURE_HOME__")
    # Recortar si es muy largo
    if len(text) > MAX_CONTENT_LEN:
        text = text[:MAX_CONTENT_LEN] + "…[recortado-fixture]"
    return text


def _sanitize_json_field(data: dict, field: str) -> None:
    """Sanitiza un campo string dentro de un dict JSON in-place."""
    if field in data and isinstance(data[field], str):
        data[field] = _sanitize_text(data[field])


def _sanitize_chat_message(chat_message_json: str) -> str:
    """Sanitiza un chat_message JSON: recorta content y sanitiza paths."""
    try:
        cm = json.loads(chat_message_json)
    except (json.JSONDecodeError, TypeError):
        return chat_message_json

    # Sanitizar content
    content = cm.get("content")
    if isinstance(content, str):
        cm["content"] = _sanitize_text(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if "text" in item and isinstance(item["text"], str):
                    item["text"] = _sanitize_text(item["text"])
                if "input" in item and isinstance(item["input"], dict):
                    for k, v in item["input"].items():
                        if isinstance(v, str):
                            item["input"][k] = _sanitize_text(v)

    return json.dumps(cm)


def _sanitize_tool_call_json(tool_call_json: str) -> str:
    """Sanitiza un tool_call_json: recorta y sanitiza paths."""
    try:
        tc = json.loads(tool_call_json)
    except (json.JSONDecodeError, TypeError):
        return tool_call_json

    _sanitize_chat_field(tc, "title")
    content = tc.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                _sanitize_chat_field(item, "text")
                _sanitize_chat_field(item, "path")
                _sanitize_chat_field(item, "newText")
                _sanitize_chat_field(item, "oldText")
    raw_input = tc.get("rawInput")
    if isinstance(raw_input, dict):
        for k, v in raw_input.items():
            if isinstance(v, str):
                raw_input[k] = _sanitize_text(v)

    return json.dumps(tc)


def _sanitize_chat_field(data: dict, field: str) -> None:
    """Sanitiza un campo string de un dict in-place."""
    if field in data and isinstance(data[field], str):
        data[field] = _sanitize_text(data[field])


CREATE_SESSIONS = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  working_directory TEXT NOT NULL,
  backend_type TEXT NOT NULL,
  model TEXT NOT NULL,
  agent_mode TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL,
  title TEXT,
  main_chain_id INTEGER,
  shell_last_seen_index INTEGER DEFAULT 0,
  cogs_json TEXT,
  workspace_dirs TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  metadata TEXT
)
"""

CREATE_MESSAGE_NODES = """
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  node_id INTEGER NOT NULL,
  parent_node_id INTEGER,
  chat_message TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  metadata TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(id),
  UNIQUE(session_id, node_id)
)
"""

CREATE_TOOL_CALL_STATE = """
CREATE TABLE tool_call_state (
    session_id    TEXT    NOT NULL,
    tool_call_id  TEXT    NOT NULL,
    tool_call_json     TEXT,
    tool_call_update_json TEXT,
    PRIMARY KEY (session_id, tool_call_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
)
"""


def _build():
    if not os.path.exists(REAL_DB):
        print(f"ERROR: Real DB not found at {REAL_DB}")
        return

    if os.path.exists(OUT):
        os.remove(OUT)

    con_real = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)

    # -- Extract session row --
    sess_cols = [d[1] for d in con_real.execute("PRAGMA table_info(sessions)").fetchall()]
    sess_row = con_real.execute(
        "SELECT * FROM sessions WHERE id = ?", (SESSION_ID,)
    ).fetchone()
    if sess_row is None:
        print(f"ERROR: Session '{SESSION_ID}' not found in real DB")
        con_real.close()
        return
    sess_dict = dict(zip(sess_cols, sess_row))
    # Sanitize session fields
    if sess_dict.get("working_directory"):
        sess_dict["working_directory"] = _sanitize_text(sess_dict["working_directory"])
    if sess_dict.get("title"):
        sess_dict["title"] = _sanitize_text(sess_dict["title"])
    if sess_dict.get("cogs_json"):
        sess_dict["cogs_json"] = _sanitize_text(sess_dict["cogs_json"])

    # -- Extract message_nodes --
    msg_cols = [d[1] for d in con_real.execute("PRAGMA table_info(message_nodes)").fetchall()]
    msg_rows = con_real.execute(
        "SELECT * FROM message_nodes WHERE session_id = ? ORDER BY row_id",
        (SESSION_ID,),
    ).fetchall()

    # -- Extract tool_call_state --
    tc_cols = [d[1] for d in con_real.execute("PRAGMA table_info(tool_call_state)").fetchall()]
    tc_rows = con_real.execute(
        "SELECT * FROM tool_call_state WHERE session_id = ?",
        (SESSION_ID,),
    ).fetchall()

    con_real.close()

    # -- Build fixture --
    con_out = sqlite3.connect(OUT)
    con_out.executescript(CREATE_SESSIONS)
    con_out.executescript(CREATE_MESSAGE_NODES)
    con_out.executescript(CREATE_TOOL_CALL_STATE)

    # Insert session
    sess_cols_filtered = [c for c in sess_cols if sess_dict.get(c) is not None]
    con_out.execute(
        "INSERT INTO sessions (" + ", ".join(sess_cols_filtered) + ") VALUES ("
        + ", ".join("?" * len(sess_cols_filtered)) + ")",
        [sess_dict[c] for c in sess_cols_filtered],
    )

    # Insert message_nodes (sanitized)
    for row in msg_rows:
        row_dict = dict(zip(msg_cols, row))
        # Sanitize chat_message
        row_dict["chat_message"] = _sanitize_chat_message(row_dict["chat_message"])
        # Sanitize metadata
        if row_dict.get("metadata"):
            row_dict["metadata"] = _sanitize_text(row_dict["metadata"])
        cols_filtered = [c for c in msg_cols if row_dict.get(c) is not None]
        con_out.execute(
            "INSERT INTO message_nodes (" + ", ".join(cols_filtered) + ") VALUES ("
            + ", ".join("?" * len(cols_filtered)) + ")",
            [row_dict[c] for c in cols_filtered],
        )

    # Insert tool_call_state (sanitized)
    for row in tc_rows:
        row_dict = dict(zip(tc_cols, row))
        # Sanitize tool_call_json
        if row_dict.get("tool_call_json"):
            row_dict["tool_call_json"] = _sanitize_tool_call_json(
                row_dict["tool_call_json"]
            )
        if row_dict.get("tool_call_update_json"):
            row_dict["tool_call_update_json"] = _sanitize_tool_call_json(
                row_dict["tool_call_update_json"]
            )
        cols_filtered = [c for c in tc_cols if row_dict.get(c) is not None]
        con_out.execute(
            "INSERT INTO tool_call_state (" + ", ".join(cols_filtered) + ") VALUES ("
            + ", ".join("?" * len(cols_filtered)) + ")",
            [row_dict[c] for c in cols_filtered],
        )

    con_out.commit()
    con_out.close()

    # Sanity check
    ro = sqlite3.connect("file:" + OUT + "?mode=ro", uri=True)
    n_msgs = ro.execute("SELECT COUNT(*) FROM message_nodes").fetchone()[0]
    n_tcs = ro.execute("SELECT COUNT(*) FROM tool_call_state").fetchone()[0]
    max_rowid = ro.execute("SELECT MAX(row_id) FROM message_nodes").fetchone()[0]
    roles = ro.execute(
        "SELECT chat_message FROM message_nodes"
    ).fetchall()
    from collections import Counter
    rc = Counter()
    for (cm_json,) in roles:
        try:
            cm = json.loads(cm_json)
            rc[cm.get("role", "unknown")] += 1
        except (json.JSONDecodeError, TypeError):
            rc["parse_error"] += 1
    ro.close()
    print(
        f"fixture OK: {OUT} "
        f"({n_msgs} messages, max_rowid={max_rowid}, {n_tcs} tool_calls, "
        f"roles={dict(rc)})"
    )


if __name__ == "__main__":
    _build()