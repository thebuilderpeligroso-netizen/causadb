"""Tests de recovery de sesiones — Fase 13 (ver Chronicle; docs/design_index.md).

Verifica que ``recover_session`` reconstruye el detalle completo desde la
FUENTE CRUDA (no del harvest lossy), reusando los fixtures reales de las
puntitas (Artículo IX — datos reales, no mocks):

  - opencode restaura los parts ``text`` que la puntita descarta
    (prompt de usuario, respuestas) — el gap que motivó la Fase 13.
  - gemini/claude/grok/hermes/openjarvis reusan el parse íntegro.
  - auto-detección: >1 herramienta → AmbiguousSessionError; 0 →
    SessionNotFoundError.
  - regresión del fix A2: dos sesiones claude del mismo slug → ids
    distintos.
"""

import json
import os
import shutil
import sqlite3

import pytest

from causadb._recover_session import (
    AmbiguousSessionError,
    SessionNotFoundError,
    recover_session,
    search_stories,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
LEDGER = "/tmp/ledger-para-test.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_fixture(monkeypatch, tmp_path, tools):
    """Instala fixtures y apunta las env vars de paths a ellas."""
    if "opencode" in tools:
        db = tmp_path / "opencode_fixture.db"
        shutil.copy(os.path.join(FIXTURE_DIR, "opencode_fixture.db"), db)
        monkeypatch.setenv("CAUSADB_OPENCODE_DB_PATH", str(db))
    if "gemini" in tools:
        project = tmp_path / "gemini_project"
        chats = project / "chats"
        chats.mkdir(parents=True)
        shutil.copy(
            os.path.join(FIXTURE_DIR, "gemini_session_fragment.jsonl"),
            chats / "session-2026-07-02T18-18-0d212d94.jsonl",
        )
        monkeypatch.setenv("CAUSADB_GEMINI_PROJECT_DIR", str(project))
    if "claude" in tools:
        projects = tmp_path / "claude_projects"
        shutil.copytree(os.path.join(FIXTURE_DIR, "claude_fixture"), projects)
        monkeypatch.setenv("CAUSADB_CLAUDE_PROJECTS_DIR", str(projects))
    if "grok" in tools:
        sessions = tmp_path / "grok_sessions"
        shutil.copytree(os.path.join(FIXTURE_DIR, "grok_fixture"), sessions)
        monkeypatch.setenv("CAUSADB_GROK_SESSIONS_DIR", str(sessions))
    if "hermes" in tools:
        db = tmp_path / "hermes_fixture.db"
        shutil.copy(os.path.join(FIXTURE_DIR, "hermes_fixture.db"), db)
        monkeypatch.setenv("CAUSADB_HERMES_DB_PATH", str(db))
    if "openjarvis" in tools:
        db = tmp_path / "openjarvis_fixture.db"
        shutil.copy(os.path.join(FIXTURE_DIR, "openjarvis_fixture.db"), db)
        monkeypatch.setenv("CAUSADB_OPENJARVIS_DB_PATH", str(db))


def _write_storyboard(tmp_path, tool, session_id, prompt):
    """Persiste un storyboard bajo <ledger_dir>/stories/<tool>/."""
    base = tmp_path / "stories" / tool
    base.mkdir(parents=True, exist_ok=True)
    sb = {
        "tool": tool,
        "session_id": session_id,
        "created_at": "2026-08-03T00:00:00Z",
        "turn_count": 1,
        "turns": [{"prompt": prompt, "assistant_response": "", "reasoning": []}],
        "tool_calls": [],
        "files_touched": [],
        "decisions": [],
        "errors": [],
        "tokens_used": 0,
        "duration_s": 0,
    }
    fname = session_id.replace("/", "_").replace(".jsonl", "")
    with open(base / f"{fname}.json", "w", encoding="utf-8") as fh:
        json.dump(sb, fh)


# ---------------------------------------------------------------------------
# 13.3 — opencode restaura los parts que la puntita descarta
# ---------------------------------------------------------------------------

def test_opencode_recovery_restores_dropped_text_parts(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    tool, sb = recover_session(
        LEDGER, "ses_05f83630bffe1Rzk65A7C0KPys", tool="opencode"
    )
    assert tool == "opencode"
    assert sb["session_id"] == "ses_05f83630bffe1Rzk65A7C0KPys"
    # El prompt de usuario vive en un part text que la puntita descarta.
    assert sb["turn_count"] >= 1
    assert "LedgerIndex" in sb["turns"][0]["prompt"]
    assert sb["turns"][0]["reasoning"], "reasoning restaurado"
    names = [tc["tool_name"] for tc in sb["tool_calls"]]
    assert "skill" in names, f"tool call restaurado: {names}"
    # part patch (schema real {"files": [...]}) → FILE_MODIFIED restaurado.
    assert "causadb/_index.py" in sb["files_touched"], sb["files_touched"]


def test_opencode_recovery_explicit_tool_uses_source(monkeypatch, tmp_path):
    """Con --tool explícito y store presente, no hace falta auto-detect."""
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    _, sb = recover_session(
        LEDGER, "ses_05f83630bffe1Rzk65A7C0KPys", tool="opencode"
    )
    assert sb["session_id"] == "ses_05f83630bffe1Rzk65A7C0KPys"


# ---------------------------------------------------------------------------
# 13.1 — recovery por herramienta (reuso de parse íntegro)
# ---------------------------------------------------------------------------

def test_gemini_recovery(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["gemini"])
    tool, sb = recover_session(
        LEDGER, "session-2026-07-02T18-18-0d212d94.jsonl", tool="gemini"
    )
    assert tool == "gemini"
    assert sb["turn_count"] >= 1


def test_claude_recovery(monkeypatch, tmp_path):
    projects = tmp_path / "claude_projects"
    projects.mkdir()
    _write_claude_session(projects, "synthetic-project",
                          "11111111-0000-0000-0000-000000000001.jsonl",
                          _claude_lines("Build a landing page"))
    monkeypatch.setenv("CAUSADB_CLAUDE_PROJECTS_DIR", str(projects))
    tool, sb = recover_session(
        LEDGER, "11111111-0000-0000-0000-000000000001.jsonl", tool="claude"
    )
    assert tool == "claude"
    assert sb["session_id"] == "11111111-0000-0000-0000-000000000001.jsonl"
    assert sb["turn_count"] >= 1
    assert "landing" in sb["turns"][0]["prompt"].lower()


def test_grok_recovery(monkeypatch, tmp_path):
    sessions = _write_grok_session(tmp_path)
    monkeypatch.setenv("CAUSADB_GROK_SESSIONS_DIR", str(sessions))
    tool, sb = recover_session(
        LEDGER, "019f492c-79d3-75b3-9651-95142b28c3c6", tool="grok"
    )
    assert tool == "grok"
    assert sb["turn_count"] >= 1
    assert "landing" in sb["turns"][0]["prompt"].lower()


def test_hermes_recovery(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["hermes"])
    tool, sb = recover_session(LEDGER, "20260802_101617_82f322", tool="hermes")
    assert tool == "hermes"
    assert sb["turn_count"] >= 1


def test_openjarvis_recovery(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["openjarvis"])
    tool, sb = recover_session(LEDGER, "6ee7c1284aec41c6", tool="openjarvis")
    assert tool == "openjarvis"
    assert sb["turn_count"] >= 1


# ---------------------------------------------------------------------------
# 13.2 — auto-detección
# ---------------------------------------------------------------------------

def test_auto_detect_single_tool(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    tool, sb = recover_session(LEDGER, "ses_05f83630bffe1Rzk65A7C0KPys")
    assert tool == "opencode"


def test_auto_detect_ambiguous_requires_tool(monkeypatch, tmp_path):
    """La misma sesión existe en opencode y hermes → AmbiguousSessionError."""
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    # crear sesión hermes con el MISMO session_id que opencode
    db = tmp_path / "hermes_fixture.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    con.execute("INSERT INTO sessions VALUES (?, 'model-a', 10, 20)",
                ("ses_05f83630bffe1Rzk65A7C0KPys",))
    con.execute(
        "CREATE TABLE messages (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, "
        "tool_calls TEXT, tool_name TEXT, timestamp REAL, "
        "reasoning_content TEXT, finish_reason TEXT)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'user', 'hola', 1785103949.0)",
        ("ses_05f83630bffe1Rzk65A7C0KPys",),
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'assistant', 'respuesta', 1785103951.0)",
        ("ses_05f83630bffe1Rzk65A7C0KPys",),
    )
    con.commit()
    con.close()
    monkeypatch.setenv("CAUSADB_HERMES_DB_PATH", str(db))

    with pytest.raises(AmbiguousSessionError) as exc:
        recover_session(LEDGER, "ses_05f83630bffe1Rzk65A7C0KPys")
    assert "opencode" in str(exc.value) and "hermes" in str(exc.value)


def test_session_not_found_auto(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    with pytest.raises(SessionNotFoundError):
        recover_session(LEDGER, "no-existe-en-ningun-lado")


def test_session_not_found_explicit_tool(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    with pytest.raises(SessionNotFoundError):
        recover_session(LEDGER, "no-existe", tool="opencode")


def test_explicit_tool_missing_store_clean_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CAUSADB_OPENCODE_DB_PATH", str(tmp_path / "no-existe.db"))
    with pytest.raises(SessionNotFoundError):
        recover_session(LEDGER, "cualquiera", tool="opencode")


def test_unknown_tool_rejected(monkeypatch, tmp_path):
    _env_fixture(monkeypatch, tmp_path, ["opencode"])
    with pytest.raises(ValueError):
        recover_session(LEDGER, "cualquiera", tool="no-tool")


# ---------------------------------------------------------------------------
# 13.4 — búsqueda en storyboards persistidos (Fase 12)
# ---------------------------------------------------------------------------

def test_search_stories_matches_keyword(monkeypatch, tmp_path):
    _write_storyboard(tmp_path, "opencode", "ses_A", "Refactor LedgerIndex")
    _write_storyboard(tmp_path, "gemini", "session-b.jsonl", "otra cosa")
    ledger = str(tmp_path / "ledger.log")
    matches = search_stories(ledger, "LedgerIndex")
    assert len(matches) == 1
    assert matches[0]["session_id"] == "ses_A"
    assert matches[0]["tool"] == "opencode"
    assert matches[0]["file"].endswith("ses_A.json")


def test_search_stories_reads_session_id_from_content(monkeypatch, tmp_path):
    # A6: session_id del CONTENIDO, no del filename sanitizado.
    _write_storyboard(tmp_path, "grok", "019f492c", "keywords de prueba")
    ledger = str(tmp_path / "ledger.log")
    matches = search_stories(ledger, "prueba")
    assert len(matches) == 1
    assert matches[0]["session_id"] == "019f492c"


def test_search_stories_missing_dir_returns_empty(tmp_path):
    ledger = str(tmp_path / "no-existe.log")
    assert search_stories(ledger, "cualquiera") == []


# ---------------------------------------------------------------------------
# Regresión fix A2 — sesiones claude del mismo slug → ids distintos
# ---------------------------------------------------------------------------

def _write_claude_session(projects_dir, slug, name, lines):
    d = os.path.join(projects_dir, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(line) for line in lines))


def _claude_lines(content="hola"):
    """Formato top-level verificado de Claude Code (ver
    test_harvest_source_claude.py::_write_synthetic_session)."""
    return [
        {"type": "queue-operation", "operation": "enqueue",
         "sessionId": "s1", "timestamp": "2026-07-20T15:39:59.000Z"},
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "text", "text": content}]},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:00.000Z"},
        {"type": "assistant",
         "message": {"id": "msg-01", "role": "assistant",
                     "model": "claude-sonnet-4-5",
                     "content": [
                         {"type": "thinking",
                          "thinking": "I need to plan the approach"},
                         {"type": "text", "text": "respuesta de prueba"},
                     ],
                     "usage": {"input_tokens": 120, "output_tokens": 45}},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:05.000Z"},
    ]


def _write_grok_session(tmp_path):
    """Sesión SINTÉTICA de grok (mismo formato verificado que
    test_harvest_source_grok.py). Retorna el sessions_dir."""
    d = tmp_path / "grok_sessions" / "%2Fhome%2Fjuliussb" / "019f492c-79d3-75b3-9651-95142b28c3c6"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"timestamp": 1783639086, "method": "session/update",
         "params": {"sessionId": "019f492c-79d3-75b3-9651-95142b28c3c6",
                    "update": {"sessionUpdate": "user_message_chunk",
                               "content": {"type": "text",
                                           "text": "Build a landing page"},
                               "_meta": {"modelId": "grok-4.5", "promptIndex": 0}},
                    "_meta": {"eventId": "e2"}}},
        {"timestamp": 1783639088, "method": "_x.ai/session/update",
         "params": {"sessionId": "019f492c-79d3-75b3-9651-95142b28c3c6",
                    "update": {"sessionUpdate": "turn_completed",
                               "prompt_id": "p1", "stop_reason": "normal",
                               "agent_result": "Here is the landing page code."},
                    "_meta": {"eventId": "e4"}}},
    ]
    with open(d / "updates.jsonl", "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
    with open(d / "summary.json", "w", encoding="utf-8") as fh:
        json.dump({"current_model_id": "grok-4.5"}, fh)
    return str(tmp_path / "grok_sessions")


def test_claude_two_sessions_same_slug_distinct_ids(monkeypatch, tmp_path):
    projects = tmp_path / "claude_projects"
    projects.mkdir()
    _write_claude_session(projects, "proyecto-x", "11111111-0000-0000-0000-000000000001.jsonl",
                          _claude_lines("uno"))
    _write_claude_session(projects, "proyecto-x", "11111111-0000-0000-0000-000000000002.jsonl",
                          _claude_lines("dos"))
    monkeypatch.setenv("CAUSADB_CLAUDE_PROJECTS_DIR", str(projects))

    _, sb1 = recover_session(
        LEDGER, "11111111-0000-0000-0000-000000000001.jsonl", tool="claude"
    )
    _, sb2 = recover_session(
        LEDGER, "11111111-0000-0000-0000-000000000002.jsonl", tool="claude"
    )
    assert sb1["session_id"] == "11111111-0000-0000-0000-000000000001.jsonl"
    assert sb2["session_id"] == "11111111-0000-0000-0000-000000000002.jsonl"
    assert sb1["session_id"] != sb2["session_id"]
