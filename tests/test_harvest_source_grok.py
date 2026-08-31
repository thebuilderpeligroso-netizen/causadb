"""Tests Fase 4 — Puntita Grok Build (BIT-GK.1; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = copia VERBATIM del ``updates.jsonl`` REAL de la única sesión de
``~/.grok/sessions/``; ver ``tests/fixtures/_build_grok_fixture.py``).

Formato real verificado (2026-08-02, auditoría del operador — NO
re-auditada acá): ``updates.jsonl`` = JSON-RPC ``session/update`` con
``timestamp`` (epoch SEGUNDOS), ``method`` ∈ ``session/update`` |
``_x.ai/session/update``, ``params.update.sessionUpdate`` ∈
``retry_state`` | ``user_message_chunk`` | ``turn_completed``. Los 3
``sessionUpdate`` reales (orden exacto): ``retry_state`` (failed, error
403 auth), ``user_message_chunk`` (``content.text="."``,
``_meta.modelId:"grok-4.5"``), ``turn_completed`` (``stop_reason:"error"``).

DESVIACIONES del plan / hallazgos (documentados también en el docstring
del módulo):
  - **Fuente primaria = ``updates.jsonl``**, NO ``chat_history.jsonl``:
    las 5 líneas de chat_history son ``system`` + 4 ``user`` (1
    user_info + 2 system_reminder + ``<user_query>.</user_query>``), SIN
    timestamp ni id y sin línea ``assistant`` (turno fallido). El user
    guide oficial define updates.jsonl como "the authoritative
    conversation log" → se usa esa. Contradice el sesgo del plan
    ("chat_history primaria") — documentado.
  - ``timestamp`` epoch SEGUNDOS → ``_s_to_iso``.
  - El model de la sesión se resuelve del PRIMER ``_meta.modelId`` del
    stream (vive en ``user_message_chunk``, no en ``turn_completed``);
    si no apareciera, de ``summary.json.current_model_id`` del mismo dir;
    si tampoco → NO emitir LLM_INVOKED (Art. IX: no inventar model).
  - ``signals.json`` NO EXISTE en la sesión real → sin token usage →
    ``tokens=None`` → ``response_tokens`` honesto = 0 (el motor
    ``_response_tokens`` con None da 0).
  - La ÚNICA sesión real es un turno FALLIDO (auth 403) → el harvest REAL
    de la fixture produce **0 eventos** (honesto, Art. IX). El happy-path
    se cubre con unit test SINTÉTICO explícitamente no-fixture
    (precedente ``test_synthetic_happy_path_mapping`` de claude).
  - Tipos de ``sessionUpdate`` no verificados con datos reales (ej. chunks
    de assistant/tool calls del ACP) → se SALTAN sin romper y sin emitir
    (documentado en el módulo).
  - NO hay message ids en este formato → sin marker ``__harvest_message_id``
    (el marker inédito viajaría al payload; el frozenset del núcleo está
    cerrado — ver nota en claude).

Cobertura (13 tests):
  1. detect() True con fixture / False sin dir / False sin updates.jsonl
  2. harvest de la fixture REAL → 0 eventos (turno fallido), idempotente,
     cursor NO avanzado (atomicidad Art. I — patrón claude)
  3. harvest no modifica la fixture (bytes idénticos, no crea archivos)
  4. unit test SINTÉTICO del happy-path (no-fixture): user_message_chunk +
     turn_completed ``stop_reason:"normal"`` → 1 LLM_INVOKED con literales
     estrictos (model/prompt/response_content/timestamp/duration/agent) +
     markers correctos + payload sin markers
  5. retry_state → sin evento
  6. turn_completed con stop_reason:"error" → sin evento (incluso con user
     chunk previo)
  7. model desde summary.json.current_model_id (updates sin _meta.modelId);
     updates sin ninguno → 0 eventos
  8. anti-teatro: cursor no avanza si el write falla; siguiente corrida
     re-cosecha TODO sin pérdida
  9. replay-determinismo (Artículo VI): 2 workspaces → mismo state
  10. tolerancia a línea JSON corrupta (append → no crashea, el offset no
      la salta; completada → se cosecha; prompt/duration honestos = 0 por
      la limitación del cursor por offset — patrón claude)
  11. unit test de advance_cursor: offset hasta la última línea del último
      evento escrito (patrón claude, sin last_message_id)
"""

import hashlib
import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_grok import GrokHarvestSource, _s_to_iso
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_SESSIONS_DIR = os.path.join(FIXTURE_DIR, "grok_fixture")
SESSION_RELPATH = (
    "%2Fhome%2Fjuliussb/019f492c-79d3-75b3-9651-95142b28c3c6/updates.jsonl"
)
SESSION_PATH = os.path.join(FIXTURE_SESSIONS_DIR, SESSION_RELPATH)

# Datos reales del turno fallido (auditoría 2026-08-02).
USER_CHUNK_TEXT = "."
TURN_ERROR_MSG = (
    "API error (status 403 Forbidden): permission-denied"
)
SESSION_MODEL = "grok-4.5"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_fixture(tmp_path):
    """Copia la fixture (updates.jsonl real verbatim) a un dir de sesiones
    temp."""
    sessions = tmp_path / "sessions"
    shutil.copytree(FIXTURE_SESSIONS_DIR, sessions)
    return str(sessions)


def _make_source(tmp_path, ledger_path=None):
    sessions_dir = _install_fixture(tmp_path)
    return GrokHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        sessions_dir=sessions_dir,
    )


def _fixture_bytes():
    with open(SESSION_PATH, "rb") as f:
        return f.read()


def _write_synthetic_session(
    tmp_path,
    name="updates.jsonl",
    ts_user=1783639086,
    ts_turn=1783639088,
    user_text="Build a landing page for a fast food restaurant.",
    agent_result="Here is the landing page code.",
    stop_reason="normal",
    model_id="grok-4.5",
    with_summary=True,
):
    """Sesión SINTÉTICA (explícitamente NO-fixture, precedente
    test_synthetic_happy_path_mapping de claude): ejercita el mapeo
    happy-path de Grok sobre la forma VERIFICADA del JSON-RPC
    ``session/update``. La sesión real local es un turno fallido (auth
    403), no un turno saludable."""
    d = tmp_path / "%2Fhome%2Fjuliussb" / "019f492c-79d3-75b3-9651-95142b28c3c6"
    d.mkdir(parents=True, exist_ok=True)

    user_meta = {"promptIndex": 0}
    if model_id is not None:
        user_meta = {"modelId": model_id, "promptIndex": 0}
    lines = [
        # --- user_message_chunk: prompt real (no emite evento) ---
        {"timestamp": ts_user, "method": "session/update",
         "params": {"sessionId": "019f492c-79d3-75b3-9651-95142b28c3c6",
                    "update": {"sessionUpdate": "user_message_chunk",
                               "content": {"type": "text", "text": user_text},
                               "_meta": user_meta},
                    "_meta": {"eventId": "e2"}}},
        # --- turn_completed: normal + agent_result → LLM_INVOKED ---
        {"timestamp": ts_turn, "method": "_x.ai/session/update",
         "params": {"sessionId": "019f492c-79d3-75b3-9651-95142b28c3c6",
                    "update": {"sessionUpdate": "turn_completed",
                               "prompt_id": "p1", "stop_reason": stop_reason,
                               "agent_result": agent_result},
                    "_meta": {"eventId": "e4"}}},
    ]
    with open(d / name, "w") as f:
        f.write("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
    if with_summary:
        # summary.json siempre existe en una sesión real (created_at/
        # updated_at/current_model_id) — fuente de model de fallback.
        with open(d / "summary.json", "w") as f:
            json.dump({"current_model_id": "grok-4.5"}, f)
    return str(tmp_path)


def _session_file(sessions_dir, name="updates.jsonl"):
    return os.path.join(
        sessions_dir, "%2Fhome%2Fjuliussb",
        "019f492c-79d3-75b3-9651-95142b28c3c6", name,
    )


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "grok"  # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:grok"


def test_harvest_current_live_grok_shape(tmp_path):
    """C1: current Grok emits thought/message chunks before end_turn."""
    sessions = tmp_path / "%2Fhome%2Ftest" / "019ff697-7482-7f42-b0f8-5e1951a8d046"
    sessions.mkdir(parents=True)
    lines = [
        {"timestamp": 1786548551, "method": "session/update", "params": {
            "sessionId": "019ff697-7482-7f42-b0f8-5e1951a8d046", "update": {
                "sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "ping"},
                "_meta": {"modelId": "grok-4.6"}}}},
        {"timestamp": 1786548551, "method": "session/update", "params": {
            "sessionId": "019ff697-7482-7f42-b0f8-5e1951a8d046", "update": {
                "sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "I will answer briefly."}}}},
        {"timestamp": 1786548551, "method": "session/update", "params": {
            "sessionId": "019ff697-7482-7f42-b0f8-5e1951a8d046", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}}}},
        {"timestamp": 1786548551, "method": "_x.ai/session/update", "params": {
            "sessionId": "019ff697-7482-7f42-b0f8-5e1951a8d046", "update": {
                "sessionUpdate": "turn_completed", "stop_reason": "end_turn"}}},
    ]
    (sessions / "updates.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    source = GrokHarvestSource(ledger_path=str(tmp_path / "ledger.log"), sessions_dir=str(tmp_path))
    raws = list(source.harvest(None))
    assert [raw["type"] for raw in raws] == ["REASONING_STEP", "LLM_INVOKED"]
    assert raws[0]["description"] == "I will answer briefly."
    assert raws[1]["model"] == "grok-4.6"
    assert raws[1]["prompt"] == "ping"
    assert raws[1]["response_content"] == "pong"
    assert raws[1]["__harvest_session_id"] == "019ff697-7482-7f42-b0f8-5e1951a8d046"
    assert raws[1]["__harvest_locator"].endswith("/updates.jsonl")


def test_detect_false_without_dir(tmp_path):
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        sessions_dir=str(tmp_path / "no-existe"),
    )
    assert source.detect() is False


def test_detect_false_with_empty_dir(tmp_path):
    empty = tmp_path / "vacio"
    empty.mkdir()
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        sessions_dir=str(empty),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture REAL → 0 eventos (turno fallido, Art. IX)
# ---------------------------------------------------------------------------

def test_harvest_real_fixture_zero_events(tmp_path):
    """La única sesión real es un turno FALLIDO (auth 403): retry_state
    failed + turn_completed stop_reason:error → 0 eventos. Honesto
    (Art. IX). Idempotente (2ª corrida → 0). El cursor NO avanza
    (atomicidad Art. I — patrón claude: con 0 eventos escritos no hay
    progreso que registrar)."""
    # Estructura real de la fixture (verbatim) verificada acá mismo: los 3
    # sessionUpdate en orden, el turno fallido y el modelId del chunk.
    with open(SESSION_PATH, encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) == 3
    session_updates = [
        ((l.get("params") or {}).get("update") or {}).get("sessionUpdate")
        for l in lines
    ]
    assert session_updates == ["retry_state", "user_message_chunk", "turn_completed"]
    turn = ((lines[2].get("params") or {}).get("update") or {})
    assert turn.get("stop_reason") == "error"
    chunk_meta = (((lines[1].get("params") or {}).get("update") or {}).get("_meta") or {})
    assert chunk_meta.get("modelId") == "grok-4.5"
    assert lines[0]["timestamp"] == 1783639086  # epoch SEGUNDOS (entero)

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    raws = list(source.harvest(None))
    assert raws == [], f"El turno real es fallido → 0 eventos, obtuvo {raws}"

    r1 = h.harvest_all()
    assert r1["grok"] == 0
    r2 = h.harvest_all()
    assert r2["grok"] == 0, f"2ª corrida debe dar 0 (idempotente), obtuvo {r2}"

    # Sin eventos escritos el ledger queda vacío y el cursor sin avanzar.
    assert not os.path.exists(ledger) or os.path.getsize(ledger) == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "0 eventos escritos → cursor no debe avanzar (atomicidad Art. I)"


# ---------------------------------------------------------------------------
# 3. read-only / no modifica la fixture
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest abre los jsonl en modo read-only: la fixture queda intacta
    (mismos bytes) y no se crean archivos en el dir de sesiones."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    before = _fixture_bytes()
    assert h.harvest_all()["grok"] == 0
    assert h.harvest_all()["grok"] == 0
    assert _fixture_bytes() == before, "El harvest no debe escribir en la fixture"

    # El dir de sesiones del source (copia temp) solo tiene el updates.jsonl
    files = []
    for root, _, names in os.walk(source.sessions_dir):
        for name in names:
            files.append(os.path.join(root, name))
    assert len(files) == 1, f"El harvest no debe crear archivos, obtuvo {files}"


# ---------------------------------------------------------------------------
# 4. unit test del mapeo happy-path (SINTÉTICO — explícitamente no-fixture)
# ---------------------------------------------------------------------------

def test_synthetic_happy_path_mapping(tmp_path):
    """Secuencia user_message_chunk + turn_completed (stop_reason normal +
    agent_result + model del stream) → 1 LLM_INVOKED con literales
    estrictos. SINTÉTICO (no-fixture, precedente test_synthetic_happy_path
    _mapping de claude): la sesión real local es un turno fallido."""
    sessions_dir = _write_synthetic_session(tmp_path)
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir
    )
    raws = list(source.harvest(None))

    assert len(raws) == 1, f"user chunk + turn normal → 1 evento, obtuvo {raws}"
    raw = raws[0]

    # -- literales estrictos del LLM_INVOKED --------------------------------
    assert raw["type"] == "LLM_INVOKED"
    assert raw["model"] == "grok-4.5", raw["model"]
    assert raw["prompt"] == "Build a landing page for a fast food restaurant."
    assert raw["response_content"] == "Here is the landing page code."
    assert raw["timestamp"] == _s_to_iso(1783639088), raw["timestamp"]
    assert raw["agent"] == "grok"
    # user 1783639086 → turn 1783639088 = 2000ms
    assert raw["duration_ms"] == 2000, raw["duration_ms"]
    # sin signals.json → sin token usage real → response_tokens honesto = 0
    assert raw["response_tokens"] == 0, raw["response_tokens"]

    # -- markers de cursor (reservados) -------------------------------------
    assert raw["__harvest_file"].endswith("updates.jsonl")
    assert raw["__harvest_offset"] > 0
    assert raw["__harvest_mtime"] == os.path.getmtime(
        _session_file(sessions_dir)
    )
    # NO hay message ids en este formato → __harvest_message_id NO viaja
    assert "__harvest_message_id" not in raw

    # -- vía Harvester: evento escrito sin markers en el payload ------------
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    h = Harvester(ledger, config)
    h.register_source(
        GrokHarvestSource(ledger_path=ledger, sessions_dir=sessions_dir)
    )
    assert h.harvest_all()["grok"] == 1
    assert h.harvest_all()["grok"] == 0, "2ª corrida con cursor → 0"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 2  # 1 LLM_INVOKED + 1 SESSION_SUMMARY
    ev = entries[0]["event"]
    assert ev["event_type"] == "LLM_INVOKED"
    p = ev["payload"]
    assert p["model"] == "grok-4.5"
    assert p["prompt"] == "Build a landing page for a fast food restaurant."
    assert p["response_content"] == "Here is the landing page code."
    assert p["agent"] == "grok"
    assert not any(k.startswith("__harvest_") for k in p), (
        "Los markers de cursor no viajan al payload del evento"
    )


# ---------------------------------------------------------------------------
# 5. retry_state → sin evento
# ---------------------------------------------------------------------------

def test_retry_state_skipped(tmp_path):
    """La línea ``retry_state`` (fallo/error, auth 403 real) no emite
    evento — es el primer sessionUpdate de la sesión real."""
    d = tmp_path / "%2Fhome%2Fjuliussb" / "019f492c-79d3-75b3-9651-95142b28c3c6"
    d.mkdir(parents=True, exist_ok=True)
    line = {"timestamp": 1783639086, "method": "_x.ai/session/update",
            "params": {"sessionId": "s1",
                       "update": {"sessionUpdate": "retry_state",
                                  "type": "failed", "error_type": "api",
                                  "message": TURN_ERROR_MSG},
                       "_meta": {"eventId": "e1"}}}
    with open(d / "updates.jsonl", "w") as f:
        f.write(json.dumps(line) + "\n")

    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=str(tmp_path)
    )
    assert list(source.harvest(None)) == []


# ---------------------------------------------------------------------------
# 6. turn_completed con stop_reason:"error" → sin evento
# ---------------------------------------------------------------------------

def test_turn_completed_error_skipped(tmp_path):
    """turn_completed con stop_reason:"error" no emite evento — incluso con
    user chunk previo (el turno real de la sesión es fallido: auth 403)."""
    sessions_dir = _write_synthetic_session(
        tmp_path,
        stop_reason="error",
        agent_result="API error (status 403 Forbidden): permission-denied",
    )
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir
    )
    raws = list(source.harvest(None))
    assert raws == [], f"Turno fallido → 0 eventos, obtuvo {raws}"


# ---------------------------------------------------------------------------
# 7. model desde summary.json (fallback sin _meta.modelId en el stream)
# ---------------------------------------------------------------------------

def test_model_from_summary_json(tmp_path):
    """El ``_meta.modelId`` vive SOLO en user_message_chunk; si no aparece
    en el stream, el model se resuelve de ``summary.json.current_model_id``
    del mismo dir (si existe). Si tampoco → 0 eventos (Art. IX: no inventar
    model)."""
    # updates SIN _meta.modelId + summary.json en el mismo dir → model del
    # summary.json (dato real: summary.json existe con current_model_id)
    sessions_dir = _write_synthetic_session(tmp_path, model_id=None)
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir
    )
    raws = list(source.harvest(None))
    assert len(raws) == 1, f"model debe resolverse del summary.json, obtuvo {raws}"
    assert raws[0]["type"] == "LLM_INVOKED"
    assert raws[0]["model"] == "grok-4.5", raws[0]["model"]

    # updates SIN _meta.modelId y SIN summary.json → 0 eventos (no hay model
    # honesto que atribuir)
    sessions_dir2 = _write_synthetic_session(
        tmp_path / "no-model", model_id=None, with_summary=False
    )
    source2 = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir2
    )
    assert list(source2.harvest(None)) == []


# ---------------------------------------------------------------------------
# 8. anti-teatro: cursor no avanza si el write falla
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    sessions_dir = _write_synthetic_session(tmp_path)
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = GrokHarvestSource(ledger_path=ledger, sessions_dir=sessions_dir)
    h = Harvester(ledger, config)
    h.register_source(source)

    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    assert result["grok"] == 0
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (1) sin pérdida
    r2 = h.harvest_all()
    assert r2["grok"] == 1, f"Debe re-cosechar todo, obtuvo {r2}"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 2  # 1 raw + 1 SESSION_SUMMARY


# ---------------------------------------------------------------------------
# 9. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_grok_harvest(tmp_path):
    def harvest_and_replay(workspace):
        sessions_dir = _write_synthetic_session(workspace)
        ledger = str(workspace / "ledger.log")
        config = str(workspace / "cursors.json")
        h = Harvester(ledger, config)
        h.register_source(
            GrokHarvestSource(ledger_path=ledger, sessions_dir=sessions_dir)
        )
        h.harvest_all()
        return ReplayEngine(ledger).reconstruct_state()

    state1 = harvest_and_replay(tmp_path / "w1")
    state2 = harvest_and_replay(tmp_path / "w2")
    # last_hash es no-determinista (timestamps del SESSION_SUMMARY). El
    # determinismo funcional se verifica comparando que los mismos eventos
    # y summaries existen en ambas corridas (precedente hermes, Fase 11).
    assert state1["events_applied"] == state2["events_applied"] == 2  # 1 raw + 1 SESSION_SUMMARY
    assert len(state1.get("session_summaries", [])) == len(state2.get("session_summaries", []))
    assert state1["session_summaries"][0]["tool"] == state2["session_summaries"][0]["tool"]
    assert state1["session_summaries"][0]["turn_count"] == state2["session_summaries"][0]["turn_count"]


# ---------------------------------------------------------------------------
# 10. tolerancia a línea JSON corrupta (patrón gemini/claude)
# ---------------------------------------------------------------------------

def test_corrupt_line_tolerated(tmp_path):
    """Línea parcial/corrupta al final → no crashea, el offset NO la salta,
    y al completarse la siguiente corrida la cosecha (patrón gemini/claude).
    Limitación honesta del cursor por offset: la línea completada (un
    turn_completed sin user chunk en la ventana) lleva prompt="" y
    duration_ms=0, con model resuelto del summary.json."""
    sessions_dir = _write_synthetic_session(tmp_path)
    fpath = _session_file(sessions_dir)

    # Línea corrupta (JSON truncado, sin newline) al final del archivo
    partial = ('{"timestamp":1783639089,"method":"session/update",'
               '"params":{"update":{"sessionUpdate":"turn_completed",'
               '"prompt_id":"p2","stop_reason":"normal",'
               '"agent_result":"a medio')
    with open(fpath, "a") as f:
        f.write(partial)

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    h = Harvester(ledger, config)
    h.register_source(
        GrokHarvestSource(ledger_path=ledger, sessions_dir=sessions_dir)
    )

    # No crashea: cosecha los eventos válidos previos (1 de la sesión)
    r1 = h.harvest_all()
    assert r1["grok"] == 1, f"Los eventos válidos previos se cosechan, obtuvo {r1}"

    # El offset NO pasó la línea corrupta
    with open(config) as f:
        cursors = json.load(f)
    entry = list(cursors["agent:grok"]["files"].values())[0]
    assert entry["offset"] < os.path.getsize(fpath), (
        "El offset no debe pasar la línea corrupta"
    )

    # Sin completarla, la corrida siguiente no crashea ni cosecha nada nuevo
    r2 = h.harvest_all()
    assert r2["grok"] == 0

    # La línea se completa → la siguiente corrida la cosecha (LLM_INVOKED)
    with open(fpath, "a") as f:
        f.write('"}}}' + "\n")
    r3 = h.harvest_all()
    assert r3["grok"] == 1, f"Debe cosechar la línea completada, obtuvo {r3}"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    # r1: 1 raw + 1 SESSION_SUMMARY; r3: 1 raw + 1 SESSION_SUMMARY → 4
    assert len(entries) == 4
    # El último raw LLM_INVOKED es el de la línea completada (r3); el último
    # entry del ledger es su SESSION_SUMMARY
    last = entries[-2]["event"]
    assert last["event_type"] == "LLM_INVOKED"
    assert last["payload"]["response_content"] == "a medio"
    # model del summary.json (la ventana desde el offset no tiene el
    # user_message_chunk del stream)
    assert last["payload"]["model"] == "grok-4.5"
    # Limitación documentada del cursor por offset (patrón claude): el user
    # chunk ya fue consumido en la corrida 1 → prompt="" y duration_ms=0 en
    # vez de inventar valores.
    assert last["payload"]["prompt"] == ""
    assert last["payload"]["duration_ms"] == 0


# ---------------------------------------------------------------------------
# 11. unit test de advance_cursor (offset, patrón claude — sin message ids)
# ---------------------------------------------------------------------------

def test_advance_cursor_offset_over_written_events(tmp_path):
    """El cursor avanza el offset hasta la última línea del último evento
    ESCRITO (el turn_completed) — la semántica de "el cursor avanza" del
    plan, verificada a nivel de la puntita. Sin ``last_message_id`` (no hay
    message ids en este formato)."""
    sessions_dir = _write_synthetic_session(tmp_path)
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir
    )
    raws = list(source.harvest(None))
    assert len(raws) == 1

    cursor = source.advance_cursor({}, raws)
    files = cursor["files"]
    assert len(files) == 1
    relpath = list(files)[0]
    assert relpath.endswith("updates.jsonl")
    entry = files[relpath]
    assert "last_message_id" not in entry, (
        "No hay message ids en el formato session/update"
    )

    # offset = fin de la línea del turn_completed (último evento escrito)
    fpath = os.path.join(sessions_dir, relpath)
    with open(fpath, "rb") as f:
        data = f.read()
    idx = data.find(b'"turn_completed"')
    assert idx >= 0
    line_end = data.find(b"\n", idx) + 1
    assert entry["offset"] == line_end, (
        f"offset={entry['offset']} debe ser fin de la línea turn_completed "
        f"({line_end})"
    )
    # El turn_completed es la última línea de la sesión sintética → el
    # offset queda en el fin del archivo (633 == size); nunca lo supera.
    assert entry["offset"] <= os.path.getsize(fpath)
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
    sessions_dir = _write_synthetic_session(tmp_path)
    source = GrokHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"), sessions_dir=sessions_dir
    )
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)
