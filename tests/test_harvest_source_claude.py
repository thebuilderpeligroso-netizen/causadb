"""Tests Fase 3 — Puntita Claude Code (BIT-CL.1; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = copia VERBATIM de una sesión REAL de ``~/.claude/projects/``;
ver ``tests/fixtures/_build_claude_fixture.py``).

Datos reales verificados (2026-08-02): las 2 sesiones de
``~/.claude/projects/`` (ambas del proyecto open-design, 9 líneas cada una)
tienen el único assistant con ``isApiErrorMessage: true`` +
``error: "authentication_failed"`` (auth fallida) → el harvest REAL de la
fixture produce **0 eventos** (todo salteable: queue-operation/attachment/
last-prompt/isApiErrorMessage; el user message se parsea sin evento). Esto
es lo honesto (Art. IX: no inventar datos de happy-path que no existen).

El mapeo de happy-path (thinking→REASONING_STEP, tool_use→TOOL_CALLED con
result completado por tool_use_id, text→response_content, usage→tokens) se
cubre con un **unit test SINTÉTICO explícitamente no-fixture** (mismo
precedente que ``test_step_type_mapping_unit`` de openjarvis) — no hay
sesión real con assistant exitoso en la máquina.

DESVIACIÓN DOCUMENTADA del plan/task: la task pedía "el cursor avanza
(offset >= último byte)" en el harvest real de 0 eventos. Artículo I
(atomicidad) + el patrón hermes/gemini (``advance_cursor`` solo avanza
sobre eventos efectivamente escritos — el ``_harvester`` pasa el prefijo
escrito) exigen lo contrario: con 0 eventos escritos el cursor NO avanza
(no hay progreso que registrar), la corrida es idempotente (0 → 0) y un
future assistant válido se cosecha sin pérdida. El avance del cursor sobre
eventos reales se verifica con el unit test de ``advance_cursor`` y con el
harvest sintético.

Cobertura:
  1. detect() True con fixture / False sin dir o sin jsonl
  2. harvest de la fixture REAL → 0 eventos, idempotente, sin crashear con
     la línea user; cursor NO avanzado (atomicidad, desviación documentada)
  3. unit test del mapeo happy-path con datos SINTÉTICOS (no-fixture):
     REASONING_STEP / TOOL_CALLED con result completado por ``tool_use_id``
     / LLM_INVOKED con model+prompt+response_content+response_tokens+
     duration_ms / dedup por message id (2ª corrida → 0)
  4. anti-teatro: cursor no avanza si el write falla; la corrida siguiente
     re-cosecha TODO sin pérdida
  5. replay-determinismo (Artículo VI): 2 workspaces → mismo state
  6. read-only: harvest no modifica la fixture ni crea archivos
  7. tolerancia a línea JSON corrupta (append de línea basura → no crashea,
     el offset no la salta, al completarse se cosecha)
  8. unit test de ``advance_cursor``: offset hasta la última línea del
     último evento escrito + ``last_message_id`` (patrón gemini)
"""

import hashlib
import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_claude import (
    ClaudeHarvestSource,
    _join_text_blocks,
)
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_SESSION_DIR = os.path.join(FIXTURE_DIR, "claude_fixture")
SESSION_RELPATH = (
    "-home-juliussb--local-share-open-design--od-projects-365fastfood-landing/"
    "11919c6c-9995-4d1b-b1ea-144fc3c64e8a.jsonl"
)
SESSION_PATH = os.path.join(FIXTURE_SESSION_DIR, SESSION_RELPATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_fixture(tmp_path):
    """Copia la fixture (sesión real verbatim) a un dir de proyectos temp."""
    projects = tmp_path / "projects"
    shutil.copytree(FIXTURE_SESSION_DIR, projects)
    return str(projects)


def _make_source(tmp_path, ledger_path=None):
    projects_dir = _install_fixture(tmp_path)
    return ClaudeHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        projects_dir=projects_dir,
    )


def _fixture_bytes():
    with open(SESSION_PATH, "rb") as f:
        return f.read()


def _write_synthetic_session(tmp_path, name="session-test.jsonl"):
    """Sesión SINTÉTICA (explícitamente NO-fixture, precedente
    test_step_type_mapping_unit de openjarvis): ejercita el mapeo happy-path
    de Claude Code. Estructura top-level real (queue-operation/attachment/
    user/assistant/last-prompt), pero con assistant EXITOSOS (los datos
    reales locales solo tienen auth fallida)."""
    slug = "synthetic-project"
    d = tmp_path / slug
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        # --- salteables (formato real) ---
        {"type": "queue-operation", "operation": "enqueue",
         "sessionId": "s1", "timestamp": "2026-07-20T15:39:59.000Z"},
        {"type": "queue-operation", "operation": "dequeue",
         "sessionId": "s1", "timestamp": "2026-07-20T15:39:59.500Z"},
        {"type": "attachment",
         "attachment": {"type": "hook_success", "hookName": "SessionStart:startup",
                        "content": ""},
         "uuid": "a1", "timestamp": "2026-07-20T15:40:00.100Z"},
        # --- user prompt real ---
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "text",
              "text": "Build a landing page for a fast food restaurant."}]},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:00.000Z"},
        # --- assistant 1: thinking + tool_use + text + usage ---
        {"type": "assistant",
         "message": {"id": "msg-01", "role": "assistant",
                     "model": "claude-sonnet-4-5",
                     "content": [
                         {"type": "thinking",
                          "thinking": "I need to plan the approach before calling tools"},
                         {"type": "tool_use", "id": "toolu_01", "name": "Bash",
                          "input": {"command": "ls -la"}},
                         {"type": "text", "text": "Listing the directory contents now."},
                     ],
                     "usage": {"input_tokens": 120, "output_tokens": 45,
                               "cache_creation_input_tokens": 10,
                               "cache_read_input_tokens": 5}},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:05.000Z"},
        # --- user con tool_result (pairing por tool_use_id) ---
        {"type": "user",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_01",
              "content": [{"type": "text", "text": "README.md  src  tests"}]}]},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:06.000Z"},
        # --- assistant 2: text only ---
        {"type": "assistant",
         "message": {"id": "msg-02", "role": "assistant",
                     "model": "claude-sonnet-4-5",
                     "content": [
                         {"type": "text", "text": "Here are the files in the project."}],
                     "usage": {"input_tokens": 200, "output_tokens": 30}},
         "sessionId": "s1", "timestamp": "2026-07-20T15:40:07.000Z"},
        # --- salteable ---
        {"type": "last-prompt", "lastPrompt": "...", "leafUuid": "leaf-1",
         "sessionId": "s1"},
    ]
    with open(d / name, "w") as f:
        f.write("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "claude"  # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:claude"


def test_detect_false_without_dir(tmp_path):
    source = ClaudeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        projects_dir=str(tmp_path / "no-existe"),
    )
    assert source.detect() is False


def test_detect_false_with_empty_dir(tmp_path):
    empty = tmp_path / "vacio"
    empty.mkdir()
    source = ClaudeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        projects_dir=str(empty),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture REAL → 0 eventos (todo salteable)
# ---------------------------------------------------------------------------

def test_harvest_real_fixture_zero_events(tmp_path):
    """La sesión real tiene el assistant con isApiErrorMessage → 0 eventos.
    Todo el resto es salteable (queue-operation/attachment/last-prompt).
    Idempotente (2ª corrida → 0). El cursor NO avanza (atomicidad, Art. I —
    desviación documentada en el header: con 0 eventos escritos no hay
    progreso que registrar)."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = list(source.harvest(None))
    assert raws == [], f"La sesión real es auth fallida → 0 eventos, obtuvo {raws}"

    r1 = h.harvest_all()
    assert r1["claude"] == 0
    r2 = h.harvest_all()
    assert r2["claude"] == 0, f"2ª corrida debe dar 0 (idempotente), obtuvo {r2}"

    # Sin eventos escritos el ledger queda vacío y el cursor sin avanzar.
    assert not os.path.exists(ledger) or os.path.getsize(ledger) == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "0 eventos escritos → cursor no debe avanzar (atomicidad Art. I)"


def test_real_fixture_user_line_parsed(tmp_path):
    """El user message real se parsea (content → texto del bloque) sin
    generar evento (Art. IX — datos reales de la fixture verbatim)."""
    with open(SESSION_PATH, encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    user = [l for l in lines if l.get("type") == "user"][0]
    text = _join_text_blocks(user["message"]["content"])
    assert text.startswith("# Instructions (read first)"), (
        "El user real debe parsearse al texto íntegro de la sesión"
    )
    assert len(text) > 1000, "El prompt real (charter open-design) está íntegro"
    assert user["timestamp"].endswith("Z"), "Timestamp ISO 8601 UTC con Z"


# ---------------------------------------------------------------------------
# 3. unit test del mapeo happy-path (SINTÉTICO — explícitamente no-fixture)
# ---------------------------------------------------------------------------

def test_synthetic_happy_path_mapping(tmp_path):
    """Secuencia user → assistant(thinking+tool_use+text+usage) → user con
    tool_result → assistant text. Esperado: 4 eventos — REASONING_STEP,
    TOOL_CALLED (result completado por tool_use_id), LLM_INVOKED x2.

    SINTÉTICO (no-fixture, precedente test_step_type_mapping_unit): los
    datos reales locales no tienen assistant exitoso (auth fallida)."""
    projects_dir = _write_synthetic_session(tmp_path)
    source = ClaudeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), projects_dir=projects_dir
    )
    raws = list(source.harvest(None))

    types = [r["type"] for r in raws]
    assert types == ["REASONING_STEP", "TOOL_CALLED", "LLM_INVOKED", "LLM_INVOKED"], types

    # -- REASONING_STEP: thinking → subject sintetizado (8 palabras) --------
    rs = raws[0]
    assert rs["description"] == "I need to plan the approach before calling tools"
    assert rs["subject"] == "I need to plan the approach before calling"
    assert rs["step_type"] == "plan", rs["step_type"]  # heurística del motor
    assert rs["step_hash"] == hashlib.sha256(
        "I need to plan the approach before calling tools".encode("utf-8")
    ).hexdigest()
    assert rs["timestamp"] == "2026-07-20T15:40:05.000Z"
    assert rs["agent"] == "claude"

    # -- TOOL_CALLED: tool_use → result COMPLETADO por tool_use_id ----------
    tc = raws[1]
    assert tc["tool_name"] == "Bash"
    assert tc["arguments"] == {"command": "ls -la"}
    assert tc["tool_call_id"] == "toolu_01"
    # pairing explícito por id: el tool_result del user siguiente completa el
    # result que nació "" en el tool_use
    assert tc["result"] == "README.md  src  tests", (
        "El TOOL_CALLED debe tener el result completado por tool_use_id"
    )
    assert tc["timestamp"] == "2026-07-20T15:40:05.000Z"
    assert tc["agent"] == "claude"

    # -- LLM_INVOKED 1: model + prompt + response_content + tokens + duración
    l1 = raws[2]
    assert l1["model"] == "claude-sonnet-4-5"
    assert l1["prompt"] == "Build a landing page for a fast food restaurant."
    assert l1["response_content"] == "Listing the directory contents now."
    assert l1["response_tokens"] == 45, l1["response_tokens"]  # usage.output_tokens
    # 15:40:00.000Z → 15:40:05.000Z = 5000ms
    assert l1["duration_ms"] == 5000, l1["duration_ms"]
    assert l1["timestamp"] == "2026-07-20T15:40:05.000Z"
    assert l1["agent"] == "claude"

    # -- LLM_INVOKED 2: el tool_result NO actualiza el prompt (no es un
    # prompt; mismo contrato que el functionResponse de gemini) ------------
    l2 = raws[3]
    assert l2["model"] == "claude-sonnet-4-5"
    assert l2["prompt"] == "Build a landing page for a fast food restaurant.", (
        "El tool_result no debe pisar last_user_content"
    )
    assert l2["response_content"] == "Here are the files in the project."
    assert l2["response_tokens"] == 30
    # duración desde el USER prompt real (no desde el tool_result)
    assert l2["duration_ms"] == 7000, l2["duration_ms"]
    assert l2["timestamp"] == "2026-07-20T15:40:07.000Z"

    # -- dedup por message id (vía cursor del Harvester): 2ª corrida → 0 ----
    # (harvest(None) sin cursor re-barre el archivo — el dedup vive en el
    # cursor last_message_id + offset, que el Harvester administra)
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    h = Harvester(ledger, config)
    h.register_source(
        ClaudeHarvestSource(ledger_path=ledger, projects_dir=projects_dir)
    )
    assert h.harvest_all()["claude"] == 4
    assert h.harvest_all()["claude"] == 0, "2ª corrida con cursor → 0"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 5  # 4 raw + 1 SESSION_SUMMARY
    assert len({e["event"]["event_id"] for e in entries}) == 5


# ---------------------------------------------------------------------------
# 4. anti-teatro: cursor no avanza si el write falla
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    projects_dir = _write_synthetic_session(tmp_path)
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = ClaudeHarvestSource(ledger_path=ledger, projects_dir=projects_dir)
    h = Harvester(ledger, config)
    h.register_source(source)

    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    assert "claude" in result
    assert result["claude"] == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (4) sin pérdida
    r2 = h.harvest_all()
    assert r2["claude"] == 4, f"Debe re-cosechar todo, obtuvo {r2}"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 5  # 4 raw + 1 SESSION_SUMMARY
    assert len({e["event"]["event_id"] for e in entries}) == 5


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_claude_harvest(tmp_path):
    def harvest_and_replay(workspace):
        projects_dir = _write_synthetic_session(workspace)
        ledger = str(workspace / "ledger.log")
        config = str(workspace / "cursors.json")
        h = Harvester(ledger, config)
        h.register_source(
            ClaudeHarvestSource(ledger_path=ledger, projects_dir=projects_dir)
        )
        h.harvest_all()
        return ReplayEngine(ledger).reconstruct_state()

    state1 = harvest_and_replay(tmp_path / "w1")
    state2 = harvest_and_replay(tmp_path / "w2")
    # last_hash es no-determinista (timestamps del SESSION_SUMMARY). El
    # determinismo funcional se verifica comparando que los mismos eventos
    # y summaries existen en ambas corridas (precedente hermes, Fase 11).
    assert state1["events_applied"] == state2["events_applied"] == 5  # 4 raw + 1 SESSION_SUMMARY
    assert len(state1.get("session_summaries", [])) == len(state2.get("session_summaries", []))
    assert state1["session_summaries"][0]["tool"] == state2["session_summaries"][0]["tool"]
    assert state1["session_summaries"][0]["turn_count"] == state2["session_summaries"][0]["turn_count"]


# ---------------------------------------------------------------------------
# 6. read-only / no modifica la fixture
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest abre los jsonl en modo read-only: la fixture queda intacta
    (mismos bytes) y no se crean archivos en el dir de proyectos."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    before = _fixture_bytes()
    assert h.harvest_all()["claude"] == 0
    assert h.harvest_all()["claude"] == 0
    assert _fixture_bytes() == before, "El harvest no debe escribir en la fixture"

    # El dir de proyectos del source (copia temp) solo tiene el session.jsonl
    files = []
    for root, _, names in os.walk(source.projects_dir):
        for name in names:
            files.append(os.path.join(root, name))
    assert len(files) == 1, f"El harvest no debe crear archivos, obtuvo {files}"


# ---------------------------------------------------------------------------
# 7. tolerancia a línea JSON corrupta (patrón gemini)
# ---------------------------------------------------------------------------

def test_corrupt_line_tolerated(tmp_path):
    """Línea parcial/corrupta al final → no crashea, el offset NO la salta,
    y al completarse la siguiente corrida la cosecha (patrón gemini)."""
    slug = "synthetic-project"
    projects_dir = _write_synthetic_session(tmp_path, name="session-partial.jsonl")
    fpath = os.path.join(projects_dir, slug, "session-partial.jsonl")

    # Línea corrupta (JSON truncado, sin newline) al final del archivo
    partial = ('{"type":"assistant","timestamp":"2026-07-20T15:40:04.000Z",'
               '"message":{"id":"msg-p2","role":"assistant",'
               '"model":"claude-sonnet-4-5","content":['
               '{"type":"text","text":"a medio')
    with open(fpath, "a") as f:
        f.write(partial)

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    h = Harvester(ledger, config)
    h.register_source(
        ClaudeHarvestSource(ledger_path=ledger, projects_dir=projects_dir)
    )

    # No crashea: cosecha los eventos válidos previos (4 de la sesión)
    r1 = h.harvest_all()
    assert r1["claude"] == 4, f"Los eventos válidos previos se cosechan, obtuvo {r1}"

    # El offset NO pasó la línea corrupta
    with open(config) as f:
        cursors = json.load(f)
    entry = list(cursors["agent:claude"]["files"].values())[0]
    assert entry["offset"] < os.path.getsize(fpath), (
        "El offset no debe pasar la línea corrupta"
    )

    # Sin completarla, la corrida siguiente no crashea ni cosecha nada nuevo
    r2 = h.harvest_all()
    assert r2["claude"] == 0

    # La línea se completa → la siguiente corrida la cosecha (LLM_INVOKED)
    with open(fpath, "a") as f:
        f.write('"}]}}' + "\n")
    r3 = h.harvest_all()
    assert r3["claude"] == 1, f"Debe cosechar la línea completada, obtuvo {r3}"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # r1: 4 raw + 1 SESSION_SUMMARY; r3: 1 raw + 1 SESSION_SUMMARY → 7
    assert len(entries) == 7  # 4 de la sesión + 1 de la línea + 2 SESSION_SUMMARY
    # El último raw LLM_INVOKED es el de la línea completada (r3); el último
    # entry del ledger es su SESSION_SUMMARY
    last = entries[-2]["event"]
    assert last["event_type"] == "LLM_INVOKED"
    assert last["payload"]["response_content"] == "a medio"
    assert last["payload"]["model"] == "claude-sonnet-4-5"
    # Limitación documentada del cursor por offset (patrón gemini): el user
    # line ya fue consumido en la corrida 1 → el estado entre mensajes
    # (prompt/prev_timestamp) no se recupera a mitad de archivo. Honesto:
    # prompt="" y duration_ms=0 en vez de inventar valores.
    assert last["payload"]["prompt"] == ""
    assert last["payload"]["duration_ms"] == 0


# ---------------------------------------------------------------------------
# 8. unit test de advance_cursor (offset + last_message_id, patrón gemini)
# ---------------------------------------------------------------------------

def test_advance_cursor_offset_over_written_events(tmp_path):
    """El cursor avanza el offset hasta la última línea del último evento
    ESCRITO y registra ``last_message_id`` — la semántica de "el cursor
    avanza" del plan, verificada a nivel de la puntita (el Harvester solo la
    invoca con el prefijo efectivamente escrito)."""
    projects_dir = _write_synthetic_session(tmp_path)
    source = ClaudeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), projects_dir=projects_dir
    )
    raws = list(source.harvest(None))
    assert len(raws) == 4

    cursor = source.advance_cursor({}, raws)
    files = cursor["files"]
    assert len(files) == 1
    relpath = list(files)[0]
    assert relpath.endswith("session-test.jsonl")
    entry = files[relpath]
    assert entry["last_message_id"] == "msg-02"  # último assistant procesado

    # offset = fin de la línea del ÚLTIMO evento escrito (msg-02), no de las
    # líneas salteables posteriores (last-prompt) ni de la línea corrupta.
    fpath = os.path.join(projects_dir, relpath)
    with open(fpath, "rb") as f:
        data = f.read()
    idx = data.find(b'"msg-02"')
    assert idx >= 0
    line_end = data.find(b"\n", idx) + 1
    assert entry["offset"] == line_end, (
        f"offset={entry['offset']} debe ser fin de la línea msg-02 ({line_end})"
    )
    assert entry["offset"] < os.path.getsize(fpath)
    assert entry["mtime"] == os.path.getmtime(fpath)

    # Idempotencia del cursor: volver a avanzar sobre lo mismo no lo mueve.
    cursor2 = source.advance_cursor(cursor, raws)
    assert cursor2 == cursor


# ---------------------------------------------------------------------------
# FIX.GEN-B — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """harvest() debe retornar un generador (FIX.GEN-B), no una lista."""
    # La fixture REAL es un turno auth fallido → 0 eventos; para consumir
    # un raw se usa la sesión SINTÉTICA (mismo precedente que el unit test
    # del happy-path de este archivo).
    projects_dir = _write_synthetic_session(tmp_path)
    source = ClaudeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), projects_dir=projects_dir
    )
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)
