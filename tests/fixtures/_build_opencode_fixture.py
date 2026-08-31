"""Genera el fixture de opencode: ``tests/fixtures/opencode_fixture.db``.

Copia PEQUEÑA y fiel del store real ``~/.local/share/opencode/opencode.db``
(Artículo IX — datos reales, no mocks). Los campos largos (prompt de user,
output de tool) se recortan con el marcador ``[recortado-fixture]``, igual
que la fixture gemini.

La sesión real extraída es ``ses_05f83630bffe1Rzk65A7C0KPys`` (agente
``testing``): 3 mensajes y 5 parts (text, step-start, reasoning, tool,
step-finish) — un ciclo completo mínimo que ejercita ambos mapeos de la
puntita (REASONING_STEP y TOOL_CALLED).

Re-ejecutar para regenerar: ``python tests/fixtures/_build_opencode_fixture.py``
"""

import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "opencode_fixture.db")

CREATE_SESSION = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text,
  `parent_id` text, `slug` text NOT NULL, `directory` text NOT NULL,
  `path` text, `title` text NOT NULL, `version` text NOT NULL,
  `share_url` text, `summary_additions` integer, `summary_deletions` integer,
  `summary_files` integer, `summary_diffs` text, `metadata` text,
  `cost` real DEFAULT 0 NOT NULL, `tokens_input` integer DEFAULT 0 NOT NULL,
  `tokens_output` integer DEFAULT 0 NOT NULL, `tokens_reasoning` integer DEFAULT 0 NOT NULL,
  `tokens_cache_read` integer DEFAULT 0 NOT NULL, `tokens_cache_write` integer DEFAULT 0 NOT NULL,
  `revert` text, `permission` text, `agent` text, `model` text,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL,
  `time_compacting` integer, `time_archived` integer
)
"""
CREATE_MESSAGE = """
CREATE TABLE `message` (
  `id` text PRIMARY KEY, `session_id` text NOT NULL,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL,
  `data` text NOT NULL
)
"""
CREATE_PART = """
CREATE TABLE `part` (
  `id` text PRIMARY KEY, `message_id` text NOT NULL, `session_id` text NOT NULL,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL,
  `data` text NOT NULL
)
"""

# Datos reales extraídos (recortados) — embedidos para regeneración determinista.
SESSION_ROW = {'id': 'ses_05f83630bffe1Rzk65A7C0KPys',
 'project_id': 'global',
 'workspace_id': None,
 'parent_id': 'ses_068b654b2ffe770wD7KOOThmrp',
 'slug': 'gentle-tiger',
 'directory': '/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master',
 'path': 'home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master',
 'title': 'Check fix LedgerIndex tests (@testing subagent)',
 'version': 'local',
 'share_url': None,
 'summary_additions': 0,
 'summary_deletions': 0,
 'summary_files': 0,
 'summary_diffs': None,
 'metadata': None,
 'cost': 0.0,
 'tokens_input': 18530,
 'tokens_output': 46,
 'tokens_reasoning': 39,
 'tokens_cache_read': 0,
 'tokens_cache_write': 0,
 'revert': None,
 'permission': '[{"permission":"todowrite","pattern":"*","action":"deny"}]',
 'agent': 'testing',
 'model': '{"id":"deepseek-v4-flash-free","providerID":"opencode","variant":"default"}',
 'time_created': 1785103949044,
 'time_updated': 1785103951934,
 'time_compacting': None,
 'time_archived': None}

MESSAGE_ROWS = [{'id': 'msg_fa07c9cf7001Nu49Ia7Ni1ZViQ',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103949047,
  'time_updated': 1785103951935,
  'data': '{"role": "user", "time": {"created": 1785103949047}, "agent": "testing", "model": '
          '{"providerID": "opencode", "modelID": "deepseek-v4-flash-free"}, "summary": {"diffs": '
          '[]}}'},
 {'id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103949053,
  'time_updated': 1785103951931,
  'data': '{"parentID": "msg_fa07c9cf7001Nu49Ia7Ni1ZViQ", "role": "assistant", "mode": "testing", '
          '"agent": "testing", "path": {"cwd": "/home/juliussb/Recupero Linux/Proyectos/Cortex '
          'Agents/Master", "root": "/"}, "cost": 0, "tokens": {"total": 18615, "input": 18530, '
          '"output": 46, "reasoning": 39, "cache": {"write": 0, "read": 0}}, "modelID": '
          '"deepseek-v4-flash-free", "providerID": "opencode", "time": {"created": 1785103949053, '
          '"completed": 1785103951931}, "finish": "tool-calls"}'},
 {'id': 'msg_fa07ca83c001pW2Y3kQ3DdCL6F',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103951932,
  'time_updated': 1785104038530,
  'data': '{"parentID": "msg_fa07c9cf7001Nu49Ia7Ni1ZViQ", "role": "assistant", "mode": "testing", '
          '"agent": "testing", "path": {"cwd": "/home/juliussb/Recupero Linux/Proyectos/Cortex '
          'Agents/Master", "root": "/"}, "cost": 0, "tokens": {"input": 0, "output": 0, '
          '"reasoning": 0, "cache": {"read": 0, "write": 0}}, "modelID": "deepseek-v4-flash-free", '
          '"providerID": "opencode", "time": {"created": 1785103951932, "completed": '
          '1785104038530}}'}]

PART_ROWS = [{'id': 'prt_fa07c9cf8001wQ7hHkwuwbVZdv',
  'message_id': 'msg_fa07c9cf7001Nu49Ia7Ni1ZViQ',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103949049,
  'time_updated': 1785103949050,
  'data': '{"type": "text", "text": "Run the LedgerIndex tests and verify the fix works. \\n\\nThe '
          'project is at: `/home/juliussb/Recupero Linux/Proyectos/Cortex '
          'Agents/Master/causadb`\\nPython venv: `/home/juliussb/Recupero '
          'Linux/Proyectos/Co\\n…[recortado-fixture]"}'},
 {'id': 'prt_fa07ca2f10010CuFji93psPyeh',
  'message_id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103950577,
  'time_updated': 1785103950578,
  'data': '{"type": "step-start"}'},
 {'id': 'prt_fa07ca532001WdoU0CQKJUfMfB',
  'message_id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103951154,
  'time_updated': 1785103951749,
  'data': '{"type": "reasoning", "text": "The user wants me to run several test commands in '
          'sequence to verify the LedgerIndex fix works. Let me start by loading the '
          'testing-strategies skill, then run the tests as specified.", "time": {"start": '
          '1785103951154, "end": 1785103951749}}'},
 {'id': 'prt_fa07ca786001U2NTv9NbAnI4or',
  'message_id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
  'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
  'time_created': 1785103951750,
  'time_updated': 1785103951867,
  'data': '{"type": "tool", "tool": "skill", "callID": "call_00_rfioiYYb2zmsv5R0CpKN7122", '
          '"state": {"status": "completed", "input": {"name": "testing-strategies"}, "output": '
          '"<skill_content name=\\"testing-strategies\\">\\n# Skill: testing-strategies\\n\\n# '
          'Testing Strategies Skill\\n\\nThis skill provides patterns and best practices for '
          'writing effective tests.\\n\\n## When to Use\\n\\nUse thi\\n…[recortado-fixture]", '
          '"metadata": {"name": "testing-strategies", "dir": '
          '"/home/juliussb/.config/opencode/skills/testing-strategies", "truncated": false}, '
          '"title": "Loaded skill: testing-strategies", "time": {"start": 1785103951852, "end": '
          '1785103951866}}}'},
  {'id': 'prt_fa07ca83900152RbkOWSJy6Gsk',
   'message_id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
   'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
   'time_created': 1785103951929,
   'time_updated': 1785103951929,
   'data': '{"reason": "tool-calls", "type": "step-finish", "tokens": {"total": 18615, "input": '
           '18530, "output": 46, "reasoning": 39, "cache": {"write": 0, "read": 0}}, "cost": 0}'},
  # Part patch REAL (schema verificado en el store real: {"files": [...]}):
  # ejercita la restauración patch → FILE_MODIFIED de la Fase 13 (el fixture
  # original no tenía file/patch parts → la rama quedaba sin cobertura).
  {'id': 'prt_fa07cb03000000patchTest0001',
   'message_id': 'msg_fa07c9cfd001T2GNrmx04lhT6K',
   'session_id': 'ses_05f83630bffe1Rzk65A7C0KPys',
   'time_created': 1785103951900,
   'time_updated': 1785103951900,
   'data': '{"type": "patch", "hash": "8936dbcf1c431f5370e35e6d58ac7be739a530b3", "files": '
           '["causadb/_index.py", "causadb/_workspace.py"]}'}]


def _build():
    if os.path.exists(OUT):
        os.remove(OUT)
    con = sqlite3.connect(OUT)
    con.executescript(CREATE_SESSION)
    con.executescript(CREATE_MESSAGE)
    con.executescript(CREATE_PART)
    s = SESSION_ROW
    cols = list(s.keys())
    con.execute(
        "INSERT INTO session (" + ", ".join(cols) + ") VALUES ("
        + ", ".join("?" * len(cols)) + ")",
        [s[c] for c in cols],
    )
    for m in MESSAGE_ROWS:
        cols = list(m.keys())
        con.execute(
            "INSERT INTO message (" + ", ".join(cols) + ") VALUES ("
            + ", ".join("?" * len(cols)) + ")",
            [m[c] for c in cols],
        )
    for p in PART_ROWS:
        cols = list(p.keys())
        con.execute(
            "INSERT INTO part (" + ", ".join(cols) + ") VALUES ("
            + ", ".join("?" * len(cols)) + ")",
            [p[c] for c in cols],
        )
    con.commit()
    con.close()
    # Sanity: lectura read-only funciona y hay rowid
    ro = sqlite3.connect("file:" + OUT + "?mode=ro", uri=True)
    n_parts = ro.execute("SELECT COUNT(*) FROM part").fetchone()[0]
    max_rowid = ro.execute("SELECT MAX(rowid) FROM part").fetchone()[0]
    ro.close()
    print("fixture OK: %s (%d parts, max_rowid=%d)" % (OUT, n_parts, max_rowid))


if __name__ == "__main__":
    _build()
