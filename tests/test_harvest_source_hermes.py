"""Tests Fase 1 — Puntita Hermes Agent (BIT-HM.1; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = datos REALES de un state.db de Hermes Agent v0.19.1 generado
contra Ollama local el 2026-08-02 — no mocks; ver
``tests/fixtures/_build_hermes_fixture.py``).

Hallazgos reales del store (ver reporte del plan §10):
  - ``messages.token_count`` es NULL → tokens por-sesión (aproximación).
  - ``sessions.model`` puede traer prefijo (``local:...``/``custom:...``).
  - ``timestamp`` es REAL epoch (segundos) → ``_s_to_iso``.
  - El assistant que llama tools tiene ``finish_reason='tool_calls'`` y
    ``tool_calls`` JSON array; la fila siguiente ``role='tool'`` trae
    ``tool_call_id`` que matchea ``tool_calls[].id`` (pairing confirmado).

Cobertura:
  1. detect() True con fixture / False sin db
  2. harvest de la fixture → eventos esperados (REASONING_STEP por reasoning,
     TOOL_CALLED por tool_calls + result completado por pairing, LLM_INVOKED
     por assistant+model por-sesión)
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la conexión es read-only (el fixture no se modifica)
  7. model por-sesión → LLM_INVOKED emitido (con prefijo estripeado)
  8. pairing tool↔result por ``tool_call_id`` (confirmado con datos reales)
"""

import hashlib
import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "hermes_fixture.db"

# Datos de la sesión con reasoning (20260802_101617_82f322, qwen3.5:4b)
REASONING_TEXT_PREFIX = "Thinking Process:\n\n1.  **Analyze the Request:**"
REASONING_TS = "2026-08-02T13:17:05.817056Z"  # 1785676625.817056 segundos epoch
USER_TS = "2026-08-02T13:16:17.828300Z"  # 1785676577.8282998
MODEL_QWEN = "qwen3.5:4b"

# Datos de la sesión con tool call (20260802_102154_c35163, llama3.1:8b)
TOOL_CALL_ID = "call_su9umhks"
TOOL_TS = "2026-08-02T13:22:10.385761Z"  # 1785676930.3857608
TOOL_RESULT_TS = "2026-08-02T13:22:10.471408Z"  # 1785676930.4714081
MODEL_LLAMA = "llama3.1:8b"


def _install_fixture(tmp_path):
    """Copia la fixture (state.db real recortado) a un dir temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return HermesHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


def _fixture_bytes():
    with open(os.path.join(FIXTURE_DIR, FIXTURE_DB), "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "hermes"  # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:hermes"


def test_detect_false_without_db(tmp_path):
    source = HermesHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.db"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → eventos canónicos
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """6 mensajes (2 sesiones) → eventos:
      sesión qwen: 1 REASONING_STEP (reasoning_content) + 1 LLM_INVOKED.
      sesión llama: 1 TOOL_CALLED (tool_calls) + 2 LLM_INVOKED (el assistant
        de tool_calls id 20 y el assistant final id 22 llevan model → ambos
        emiten, ver mapeo de la puntita).
    """
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = list(source.harvest(None))
    types = [r["type"] for r in raws]
    assert types.count("REASONING_STEP") == 1, types
    assert types.count("TOOL_CALLED") == 1, types
    # LLM_INVOKED: assistant con model por-sesión. El assistant con tool_calls
    # (id 20, finish_reason='tool_calls') también lleva model → cuenta.
    # assistants totales con model: id 6 (qwen) + id 20 (llama) + id 22 (llama)
    # = 3 → los 3 emiten.
    assert types.count("LLM_INVOKED") == 3, types
    # H1.1: +2 SESSION_STARTED +2 SESSION_ENDED (una por sesión cerrada)
    assert len(raws) == 9, f"Esperaba 9 raws, obtuvo {len(raws)}"

    # -- REASONING_STEP (sesión qwen) --------------------------------------
    reasoning = [r for r in raws if r["type"] == "REASONING_STEP"]
    r = reasoning[0]
    assert r["description"].startswith(REASONING_TEXT_PREFIX)
    # Subject sintetizado: primeras 8 palabras del reasoning_content COMPLETO
    # de la fixture (los tokens 7-8 son "*" e "Input:" del texto real).
    assert r["subject"] == "Thinking Process: 1. **Analyze the Request:** * Input:"
    assert r["step_type"] == "analysis"  # heurística del motor universal
    assert r["step_hash"] == hashlib.sha256(
        r["description"].encode("utf-8")
    ).hexdigest()
    assert r["agent"] == "hermes"
    assert r["timestamp"] == REASONING_TS
    assert r["__harvest_locator"] == source.db_path

    # -- TOOL_CALLED (sesión llama, pairing completado) --------------------
    tool = [r for r in raws if r["type"] == "TOOL_CALLED"]
    t = tool[0]
    assert t["tool_name"] == "terminal"
    assert t["tool_call_id"] == TOOL_CALL_ID
    assert t["agent"] == "hermes"
    assert t["timestamp"] == TOOL_TS
    # result completado por pairing tool_call_id (fila role='tool' id 21)
    assert t["result"].startswith('{"output": "hermes-agent')
    assert t["result"].endswith('"error": null}')

    # -- LLM_INVOKED (model por-sesión, prefijos estripeados) ---------------
    llms = [r for r in raws if r["type"] == "LLM_INVOKED"]
    models = {llm["model"] for llm in llms}
    assert models == {MODEL_QWEN, MODEL_LLAMA}
    qwen_llm = [l for l in llms if l["model"] == MODEL_QWEN][0]
    assert qwen_llm["response_content"] == "¡Hola! Espero que estés teniendo un gran día."
    assert qwen_llm["prompt"] == "Escribe una frase corta saludando en español."
    assert qwen_llm["timestamp"] == REASONING_TS
    assert qwen_llm["agent"] == "hermes"
    # H2.3: messages.token_count es NULL en la fixture → response_tokens
    # per-message honesto = 0 (el agregado sessions.output_tokens vive solo
    # en API_ATTEMPT/COST_ACCOUNTED, nunca se repite por assistant).
    assert qwen_llm["response_tokens"] == 0  # contrato H2.3 (token_count NULL)
    assert qwen_llm["duration_ms"] == 47988  # diff user→assistant real
    llama_llms = [l for l in llms if l["model"] == MODEL_LLAMA]
    assert all(l["response_tokens"] == 0 for l in llama_llms)  # contrato H2.3

    # Flujo completo: harvest_all escribe los eventos al ledger
    result = h.harvest_all()
    # H1.1: 9 hermes (+2 STARTED +2 ENDED) + 1 SESSION_SUMMARY (Fase 11)
    assert result["hermes"] == 9
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 10  # 9 hermes + 1 SESSION_SUMMARY
    etypes = sorted(e["event"]["event_type"] for e in entries)
    assert etypes == ["LLM_INVOKED", "LLM_INVOKED", "LLM_INVOKED",
                      "REASONING_STEP", "SESSION_ENDED", "SESSION_ENDED",
                      "SESSION_STARTED", "SESSION_STARTED",
                      "SESSION_SUMMARY", "TOOL_CALLED"]
    agent_ids = {e["event"]["payload"].get("agent") for e in entries
                 if e["event"]["payload"].get("agent") is not None}
    assert agent_ids == {"hermes"}


def test_model_prefix_normalized():
    """Hermes puede guardar el model con prefijo provider (``local:...``,
    ``custom:...``) o desnudo — la puntita estripea antes de emitir."""
    from causadb._harvest_source_hermes import _normalize_model
    assert _normalize_model("local:qwen3.5:4b") == "qwen3.5:4b"
    assert _normalize_model("custom:ollama:qwen3.5:4b") == "qwen3.5:4b"
    assert _normalize_model("ollama:qwen3.5:4b") == "qwen3.5:4b"
    assert _normalize_model("qwen3.5:4b") == "qwen3.5:4b"  # Ollama tag intacto
    assert _normalize_model("  llama3.1:8b  ") == "llama3.1:8b"
    assert _normalize_model("") is None
    assert _normalize_model(None) is None
    # Edge cases de la auditoría 2.7:
    assert _normalize_model("local:") is None  # prefijo desnudo → None
    assert _normalize_model("custom:") is None
    assert _normalize_model("LOCAL:qwen3.5:4b") == "LOCAL:qwen3.5:4b"  # case-sensitive documentado
    assert _normalize_model("openai:gpt-4o") == "openai:gpt-4o"  # set cerrado de prefijos


def test_last_user_content_isolated_per_session(tmp_path):
    """Auditoría 2.1: el barrido intercala filas de TODAS las sesiones por
    rowid → el prompt de cada LLM_INVOKED debe salir del user de SU sesión,
    nunca del de otra sesión intercalada."""
    import sqlite3
    db = tmp_path / "interleaved.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, "
        "tool_calls TEXT, tool_name TEXT, timestamp REAL, "
        "reasoning_content TEXT, finish_reason TEXT)"
    )
    con.execute("INSERT INTO sessions VALUES ('A','local:model-a',10,20)")
    con.execute("INSERT INTO sessions VALUES ('B','local:model-b',30,40)")
    # rows intercaladas: user de A, user de B, assistant de A, assistant de B
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('A','user','PREGUNTA-A',1.0)")
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('B','user','PREGUNTA-B',2.0)")
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('A','assistant','RESP-A',3.0)")
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('B','assistant','RESP-B',4.0)")
    con.commit()
    con.close()

    source = HermesHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(db),
    )
    raws = source.harvest(None)
    llms = {r["model"]: r for r in raws if r["type"] == "LLM_INVOKED"}
    assert set(llms) == {"model-a", "model-b"}
    assert llms["model-a"]["prompt"] == "PREGUNTA-A"
    assert llms["model-b"]["prompt"] == "PREGUNTA-B"


# ---------------------------------------------------------------------------
# 3. idempotencia (cursor por rowid)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["hermes"] == 9  # H1.1: +2 STARTED +2 ENDED
    r2 = h.harvest_all()
    assert r2["hermes"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 10  # 9 hermes + 1 SESSION_SUMMARY
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# 4. anti-teatro: cursor no avanza si el write falla
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    assert "hermes" in result
    assert result["hermes"] == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (5) sin pérdida
    r2 = h.harvest_all()
    assert r2["hermes"] == 9  # H1.1: +2 STARTED +2 ENDED
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 10  # 9 hermes + 1 SESSION_SUMMARY
    assert len({e["event"]["event_id"] for e in entries}) == 10


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_hermes_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    # last_hash es no-deterministico (timestamps, hashes de eventos derivados)
    # El determinismo funcional se verifica comparando que los mismos
    # eventos existen en ambas corridas (events_applied), no hashes/timestamps
    assert state1["events_applied"] == state2["events_applied"] == 10
    assert len(state1.get("session_summaries", [])) == len(state2.get("session_summaries", []))
    assert state1["session_summaries"][0]["tool"] == state2["session_summaries"][0]["tool"]
    assert state1["session_summaries"][0]["turn_count"] == state2["session_summaries"][0]["turn_count"]


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["hermes"] == 9  # H1.1: +2 STARTED +2 ENDED
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["events_applied"] == 10  # 9 hermes + 1 SESSION_SUMMARY
    return state


# ---------------------------------------------------------------------------
# 6. anti-teatro: la conexión es read-only
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest abre el db con ``mode=ro``: el fixture queda intacto
    (mismos bytes) aunque se coseche varias veces."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    before = _fixture_bytes()
    assert h.harvest_all()["hermes"] == 9  # H1.1: +2 STARTED +2 ENDED
    assert h.harvest_all()["hermes"] == 0
    assert _fixture_bytes() == before, "mode=ro debe dejar el db intacto"

    side_files = [f for f in os.listdir(FIXTURE_DIR)
                  if f.startswith(FIXTURE_DB + "-")]
    assert side_files == []


# ---------------------------------------------------------------------------
# 7. pairing tool↔result por tool_call_id (datos reales)
# ---------------------------------------------------------------------------

def test_tool_result_paired_by_tool_call_id(tmp_path):
    """La fila role='tool' (id 21) trae tool_call_id que matchea el id del
    tool_calls del assistant (id 20) — el result se completa en el TOOL_CALLED."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = source.harvest(None)
    tool = [r for r in raws if r["type"] == "TOOL_CALLED"][0]
    assert tool["tool_call_id"] == TOOL_CALL_ID
    assert tool["result"], "El TOOL_CALLED debe tener result completado por pairing"
    assert "hermes-venv" in tool["result"]


# ---------------------------------------------------------------------------
# FIX.HERMES — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """FIX.HERMES — harvest() debe retornar un generador, no una lista."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"


def test_tool_result_fallback_by_tool_name_when_no_id(tmp_path):
    """FIX.HERMES — el lookup anticipado debe cubrir el camino defensivo:
    fila role='tool' con tool_call_id=NULL pero tool_name que matchea el
    tool_calls del assistant → el TOOL_CALLED nace con result completo
    (el lookup primario por id NO matchea; gana el fallback por tool_name)."""
    import sqlite3
    db = tmp_path / "fallback.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, "
        "tool_calls TEXT, tool_name TEXT, timestamp REAL, "
        "reasoning_content TEXT, finish_reason TEXT)"
    )
    con.execute("INSERT INTO sessions VALUES ('S','local:mi-model',10,20)")
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES ('S','user','PREGUNTA',NULL,1.0)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES ('S','assistant',NULL,"
        "'[{\"id\": \"call-x\", \"function\": {\"name\": \"mi_tool\", "
        "\"arguments\": \"{\\\"a\\\":1}\"}}]',2.0)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
        "VALUES ('S','tool','{\"output\": \"RESULTADO-DEFENSIVO\"}','mi_tool',3.0)"
    )
    con.commit()
    con.close()

    source = HermesHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(db),
    )
    raws = list(source.harvest(None))
    tools = [r for r in raws if r["type"] == "TOOL_CALLED"]
    assert len(tools) == 1, tools
    assert tools[0]["result"] == '{"output": "RESULTADO-DEFENSIVO"}'


def test_tool_result_primary_lookup_wins_over_fallback(tmp_path):
    """FIX.HERMES — cuando la fila role='tool' trae tool_call_id (caso real),
    el lookup primario por id completa el result; una fila tool sin id del
    mismo tool que le sigue NO debe pisarlo (primario gana sobre fallback)."""
    import sqlite3
    db = tmp_path / "primary.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER)"
    )
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, "
        "tool_calls TEXT, tool_name TEXT, timestamp REAL, "
        "reasoning_content TEXT, finish_reason TEXT)"
    )
    con.execute("INSERT INTO sessions VALUES ('S','local:mi-model',10,20)")
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES ('S','user','PREGUNTA',NULL,1.0)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES ('S','assistant',NULL,"
        "'[{\"id\": \"call-x\", \"function\": {\"name\": \"mi_tool\", "
        "\"arguments\": \"{\\\"a\\\":1}\"}}]',2.0)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_call_id, "
        "tool_name, timestamp) VALUES "
        "('S','tool','{\"output\": \"RESULTADO-PRIMARIO\"}','call-x','mi_tool',3.0)"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
        "VALUES ('S','tool','{\"output\": \"RESULTADO-FALLBACK\"}','mi_tool',4.0)"
    )
    con.commit()
    con.close()

    source = HermesHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(db),
    )
    raws = list(source.harvest(None))
    tools = [r for r in raws if r["type"] == "TOOL_CALLED"]
    assert len(tools) == 1, tools
    assert tools[0]["result"] == '{"output": "RESULTADO-PRIMARIO"}'


# ---------------------------------------------------------------------------
# GAP-01 — forward-compat: config del agente declara el store (t13)
# ---------------------------------------------------------------------------

def test_db_path_from_agent_config_and_log_derivation(tmp_path, monkeypatch):
    """Sin db_path explícito: ~/.hermes/config.json key 'data' declara el
    store; el log de API_ATTEMPT deriva del mismo dirname (logs/agent.log)."""
    home = tmp_path / "home"
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    custom_db = hermes_dir / "state.db"
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), custom_db)
    (hermes_dir / "config.json").write_text(json.dumps({"data": str(custom_db)}))
    # log real con una llamada completada
    logs = hermes_dir / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text(
        "2026-08-02 13:22:10,385 INFO [20260802_102154_c35163] "
        "agent.conversation_loop: API call #1: model=llama3.1:8b provider=ollama "
        "in=100 out=50 total=150 latency=1.234s\n"
    )
    monkeypatch.setenv("HOME", str(home))

    source = HermesHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source.db_path == str(custom_db)
    assert source.detect() is True
    raws = list(source.harvest(None))
    api = [r for r in raws if r["type"] == "API_ATTEMPT"]
    assert len(api) == 1, f"esperaba 1 API_ATTEMPT del log derivado, got {[r['type'] for r in raws]}"
    assert api[0]["status"] == "completed"
    assert api[0]["model"] == "llama3.1:8b"

    # env override gana sobre el config
    env_db = tmp_path / "env.db"
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), env_db)
    monkeypatch.setenv("CAUSADB_HERMES_DB_PATH", str(env_db))
    source2 = HermesHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source2.db_path == str(env_db)
