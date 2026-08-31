"""Tests Fase 2 — Puntita gemini-cli (ver docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture REAL extraída de ``~/.gemini/tmp/cortex-agents/chats/``, no mocks).

Cobertura:
  1. detect() True con fixture / False sin el dir del proyecto
  2. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  3. archivo que crece → solo lo nuevo
  4. línea parcial (a medio escribir) → no rompe y el offset no la salta
  5. líneas $set/metadata → no generan eventos
  6. anti-teatro: cursor no avanza si el ledger write falla
  7. anti-teatro fallo parcial: algunos escritos, luego falla → solo
     avanzan los escritos (los no escritos se re-cosechan sin duplicar)
  8. replay-determinismo (Artículo VI): mismo harvest → mismo state
"""

import json
import os

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_gemini import GeminiHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_FILE = "gemini_session_fragment.jsonl"


def _load_fixture_lines():
    with open(os.path.join(FIXTURE_DIR, FIXTURE_FILE)) as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def _install_fixture(tmp_path):
    """Copia la fixture real a un dir de proyecto temporal (con chats/)."""
    project_dir = tmp_path / "project"
    chats = project_dir / "chats"
    chats.mkdir(parents=True)
    lines = _load_fixture_lines()
    session = chats / "session-2026-07-02T18-18-0d212d94.jsonl"
    session.write_text("\n".join(lines) + "\n")
    return str(project_dir)


def _make_source(tmp_path, ledger_path=None):
    project_dir = _install_fixture(tmp_path)
    return GeminiHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        project_dir=project_dir,
    )


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "gemini"          # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:gemini"


def test_detect_false_without_project_dir(tmp_path):
    source = GeminiHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        project_dir=str(tmp_path / "no-existe"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2+5. harvest básico sobre la fixture real: $set/metadata no generan eventos
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """7 líneas (metadata, $set x3, user, gemini thoughts, re-emisión con
    toolCalls) → exactamente 4 eventos: 2 REASONING_STEP (el mensaje real
    tiene 2 thoughts) + TOOL_CALLED + LLM_INVOKED. Los $set/metadata NO
    generan eventos; la re-emisión del mensaje con toolCalls NO duplica."""
    source = _make_source(tmp_path)
    raws = list(source.harvest(None))

    assert len(raws) == 4, f"Esperaba 4 eventos, obtuve {len(raws)}: {[r['type'] for r in raws]}"
    types = [r["type"] for r in raws]
    assert types == ["REASONING_STEP", "REASONING_STEP", "TOOL_CALLED", "LLM_INVOKED"]

    # --- REASONING_STEPs: subjects reales de la sesión ---
    r1 = raws[0]
    assert r1["step_type"] == "reflection"          # "Re-examining" → reflection
    assert r1["subject"] == "Re-examining Search Frustration"
    assert len(r1["step_hash"]) == 64               # sha256 hex
    assert r1["step_hash"] == r1["step_hash"].lower()
    assert "agent" in r1 and r1["agent"] == "gemini"

    r1b = raws[1]
    assert r1b["subject"] == "Reviewing File Location"
    assert r1b["step_type"] == "reflection"         # "Reviewing" → reflection
    assert r1b["step_hash"] != r1["step_hash"]      # contenidos distintos

    # --- TOOL_CALLED: read_file con args y result completos ---
    r2 = raws[2]
    assert r2["tool_name"] == "read_file"
    assert "file_path" in r2["arguments"]
    assert r2["result"]  # result del functionResponse normalizado
    assert r2["tool_call_id"]

    # --- LLM_INVOKED: model real + prompt del user anterior + duration_ms ---
    r3 = raws[3]
    assert r3["model"] == "gemini-3.1-flash-lite"
    assert r3["response_tokens"] == 28
    # prompt = contenido del mensaje user de la fixture (recortado)
    user_line = json.loads(_load_fixture_lines()[2])
    user_text = "".join(p.get("text", "") for p in user_line["content"])
    assert r3["prompt"] == user_text
    # 18:18:50.511Z - 18:18:45.753Z = 4758ms
    assert r3["duration_ms"] == 4758
    assert r3["response_content"] == ""  # el mensaje gemini no tenía contenido


# ---------------------------------------------------------------------------
# 2. dos corridas = 0 dups (a través del Harvester completo)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["gemini"] == 4

    r2 = h.harvest_all()
    assert r2["gemini"] == 0, f"2ª corrida debe devolver 0, obtuvo {r2}"

    # Ledger: 4 entries de la fixture + 1 SESSION_SUMMARY (Fase 11), ids únicos
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 5
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 5


# ---------------------------------------------------------------------------
# 3. archivo que crece → solo lo nuevo
# ---------------------------------------------------------------------------

def test_file_grows_only_new_lines(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["gemini"] == 4

    # El archivo crece: se apendea un mensaje gemini nuevo con 1 thought
    chats = os.path.join(source.project_dir, "chats")
    session = os.path.join(chats, "session-2026-07-02T18-18-0d212d94.jsonl")
    new_line = json.dumps({
        "id": "new-message-1",
        "timestamp": "2026-07-02T18:20:00.000Z",
        "type": "gemini",
        "content": "",
        "thoughts": [{"subject": "Planning the next step", "description": "nuevo razonamiento"}],
        "model": None,
    })
    with open(session, "a") as f:
        f.write(new_line + "\n")

    r2 = h.harvest_all()
    assert r2["gemini"] == 1, f"Solo lo nuevo, obtuvo {r2}"
    assert r2["gemini"] == 1

    # 4 fixture + 1 SESSION_SUMMARY + 1 nuevo (REASONING_STEP, sin
    # LLM_INVOKED → esa corrida NO genera SESSION_SUMMARY) = 6
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 6  # 4 de la fixture + 1 SESSION_SUMMARY + 1 nuevo
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 6  # sin duplicados


# ---------------------------------------------------------------------------
# 4. línea parcial no rompe y el offset no la salta
# ---------------------------------------------------------------------------

def test_partial_line_tolerated_and_recovered(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["gemini"] == 4

    chats = os.path.join(source.project_dir, "chats")
    session = os.path.join(chats, "session-2026-07-02T18-18-0d212d94.jsonl")

    # Línea a medio escribir (gemini-cli escribiendo): JSON truncado
    partial = '{"id":"partial-1","timestamp":"2026-07-02T18:21:00.000Z","type":"gemini","content":"","thoughts":[{"subject":"Analyzing x","description":"a mitad'
    with open(session, "a") as f:
        f.write(partial)  # sin newline

    # No rompe, y el offset NO la salta: 0 eventos nuevos
    r2 = h.harvest_all()
    assert r2["gemini"] == 0

    # El cursor quedó al final de la última línea completa parseable
    with open(config) as f:
        cursors = json.load(f)
    entry = cursors["agent:gemini"]["files"]["session-2026-07-02T18-18-0d212d94.jsonl"]
    size = os.path.getsize(session)
    assert entry["offset"] < size, "El offset no debe pasar la línea parcial"

    # La línea se completa → la siguiente corrida la cosecha
    # (nota: el prefijo `"` cierra el string `"a mitad` truncado)
    with open(session, "a") as f:
        f.write('"}],"model":null}' + "\n")
    r3 = h.harvest_all()
    assert r3["gemini"] == 1, f"Debe cosechar la línea completada, obtuvo {r3}"
    assert r3["gemini"] == 1


# ---------------------------------------------------------------------------
# 6. anti-teatro: cursor no avanza si el write falla (fallo total)
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    # El ledger write falla SIEMPRE → no se avanza cursor, no se pierde nada
    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    # harvest_all no crashea (aislamiento por fuente, auditoría I.2)
    assert "gemini" in result
    # El cursor NO avanzó: archivo ausente, vacío o `{}` (mismo contrato
    # que test_harvester.py::test_harvester_handles_missing_source)
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (4) sin pérdida
    r2 = h.harvest_all()
    assert r2["gemini"] == 4
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 5  # 4 raw + 1 SESSION_SUMMARY
    assert len({e["event"]["event_id"] for e in entries}) == 5


# ---------------------------------------------------------------------------
# 7. anti-teatro fallo parcial: solo avanzan los escritos
# ---------------------------------------------------------------------------

def _write_three_single_event_session(chats_dir, name="session-x.jsonl"):
    """Session sintética con 3 mensajes, cada uno → exactamente 1 evento
    (estructura idéntica al formato real del oplog)."""
    lines = [
        json.dumps({"sessionId": "test", "kind": "main"}),
        json.dumps({"$set": {"lastUpdated": "2026-07-01T00:00:00Z"}}),
        # m1 → REASONING_STEP (thought, sin model)
        json.dumps({"id": "m1", "timestamp": "2026-07-01T00:00:01Z", "type": "gemini",
                    "content": "", "model": None,
                    "thoughts": [{"subject": "Planning x", "description": "razonamiento A"}]}),
        # m2 → TOOL_CALLED (toolCalls, sin model ni thoughts)
        json.dumps({"id": "m2", "timestamp": "2026-07-01T00:00:02Z", "type": "gemini",
                    "content": "",
                    "toolCalls": [{"id": "t1", "name": "bash", "args": {"cmd": "ls"},
                                   "result": [{"functionResponse": {"response": {"output": "ok"}}}]}]}),
        # m3 → LLM_INVOKED (model, sin thoughts ni tools)
        json.dumps({"id": "m3", "timestamp": "2026-07-01T00:00:03Z", "type": "gemini",
                    "content": "resp", "model": "m", "tokens": {"output": 5}}),
    ]
    with open(os.path.join(chats_dir, name), "w") as f:
        f.write("\n".join(lines) + "\n")


def test_cursor_advances_only_over_written_events(tmp_path):
    """Fallo parcial: el 2º evento falla → solo avanza lo escrito (m1).
    La corrida siguiente re-cosecha m2+m3 sin duplicar m1."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    project_dir = tmp_path / "project"
    chats = project_dir / "chats"
    chats.mkdir(parents=True)
    _write_three_single_event_session(chats)

    source = GeminiHarvestSource(ledger_path=ledger, project_dir=str(project_dir))
    h = Harvester(ledger, config)
    h.register_source(source)

    # El append falla en la 2ª llamada (write_events fail-fast → count=1).
    # La 1ª escritura es REAL (va al ledger); la 2ª lanza OSError.
    import unittest.mock as um
    calls = {"n": 0}
    real_append = h._writer.append

    def _flaky_append(event):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("partial disk failure")
        return real_append(event)

    with um.patch.object(h._writer, "append", side_effect=_flaky_append):
        result = h.harvest_all()

    assert result["gemini"] == 1  # solo el primer evento escrito
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 1
    first_id = entries[0]["event"]["event_id"]

    # El cursor solo avanzó sobre lo escrito: offset al final de la línea m1
    with open(config) as f:
        cursors = json.load(f)
    entry = cursors["agent:gemini"]["files"]["session-x.jsonl"]
    assert entry["offset"] < os.path.getsize(os.path.join(chats, "session-x.jsonl"))

    # Corrida siguiente (write OK): cosecha m2+m3 → sin duplicar m1. Total
    # 4 = m1 + m2 + m3 (raw) + 1 SESSION_SUMMARY (m3 es LLM_INVOKED)
    r2 = h.harvest_all()
    assert r2["gemini"] == 2, f"Debe re-cosechar lo no escrito, obtuvo {r2}"
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 4
    ids = [e["event"]["event_id"] for e in entries]
    assert len(set(ids)) == 4
    assert ids[0] == first_id  # m1 no se duplicó


# ---------------------------------------------------------------------------
# 8. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_gemini_harvest(tmp_path):
    """Mismo harvest sobre dos ledgers frescos → mismo estado replay.
    (Artículo VI: la acción debe ser reproducible en replay.)"""
    def harvest_and_replay(workspace):
        ledger = str(workspace / "ledger.log")
        config = str(workspace / "cursors.json")
        project_dir = _install_fixture(workspace)
        source = GeminiHarvestSource(ledger_path=ledger, project_dir=project_dir)
        h = Harvester(ledger, config)
        h.register_source(source)
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
# FIX.GEN-B — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """harvest() debe retornar un generador (FIX.GEN-B), no una lista."""
    source = _make_source(tmp_path)
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)


# ---------------------------------------------------------------------------
# C1.2 — session_id canónico (UUID de la línea kind:main) + locator
# ---------------------------------------------------------------------------

def test_session_id_is_canonical_uuid(tmp_path, monkeypatch):
    """C1.2: el session_id del ledger debe ser el sessionId UUID de la línea
    kind:main de gemini-cli (identidad canónica de la sesión), NO el nombre
    de archivo. Consistente con el resto de puntitas (opencode, cursor,
    codex, windsurf) que capturan el session_id real de la fuente."""
    source = _make_source(tmp_path)
    raws = list(source.harvest(None))
    assert len(raws) == 4
    # El fixture declara sessionId 0d212d94-26ec-4ee7-b6b6-d3f1a902caa8
    for r in raws:
        assert r["__harvest_session_id"] == "0d212d94-26ec-4ee7-b6b6-d3f1a902caa8", \
            f"session_id debe ser el UUID canónico, got {r.get('__harvest_session_id')}"
        assert r["__harvest_locator"] == "session-2026-07-02T18-18-0d212d94.jsonl", \
            "el archivo JSONL crudo debe quedar como locator separado"


def test_session_id_canonical_uuid_reaches_ledger_payload(tmp_path):
    """El UUID canónico debe viajar al payload del ledger como session_id y
    el archivo como session_locator, recuperables vía query exacta."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    h.harvest_all()

    entries = []
    with open(ledger) as f:
        for ln in f:
            if ln.strip():
                entries.append(json.loads(ln))
    session_events = [
        e["event"] for e in entries if e["event"]["payload"].get("session_id")
    ]
    assert session_events, "debe haber eventos con session_id"
    for ev in session_events:
        assert ev["payload"]["session_id"] == "0d212d94-26ec-4ee7-b6b6-d3f1a902caa8"
        # SESSION_SUMMARY es derivado por el summarizer (no raw) → no lleva
        # locator. Los eventos raw (cosechados de la sesión) sí lo llevan.
        if ev["event_type"] != "SESSION_SUMMARY":
            assert ev["payload"]["session_locator"] == "session-2026-07-02T18-18-0d212d94.jsonl"


def test_session_id_fallback_to_filename_without_kind_main(tmp_path):
    """Archivos sin línea kind:main (viejos/malformados) → fallback al nombre
    de archivo como session_id (no rompe el contrato)."""
    ledger = str(tmp_path / "ledger.log")
    project_dir = tmp_path / "project"
    chats = project_dir / "chats"
    chats.mkdir(parents=True)
    # Sin kind:main: solo $set + user + gemini
    lines = [
        json.dumps({"$set": {"lastUpdated": "2026-07-01T00:00:00Z"}}),
        json.dumps({"id": "m1", "timestamp": "2026-07-01T00:00:01Z", "type": "user", "content": "hola"}),
        json.dumps({"id": "m2", "timestamp": "2026-07-01T00:00:02Z", "type": "gemini", "content": "resp", "model": "m"}),
    ]
    session = chats / "session-legacy-abc.jsonl"
    session.write_text("\n".join(lines) + "\n")

    source = GeminiHarvestSource(ledger_path=ledger, project_dir=str(project_dir))
    raws = list(source.harvest(None))
    assert len(raws) == 1  # solo LLM_INVOKED (m2)
    assert raws[0]["__harvest_session_id"] == "session-legacy-abc.jsonl"
    assert raws[0]["__harvest_locator"] == "session-legacy-abc.jsonl"


# ---------------------------------------------------------------------------
# GAP-01 — auto-discovery multi-store (docs/plan_gaps_01_02.md)
# ---------------------------------------------------------------------------

def _write_store(home, slug, fname, session_uuid, thought_subject):
    """Escribe un store gemini-cli real: kind:main + 1 mensaje gemini."""
    chats = os.path.join(str(home), ".gemini", "tmp", slug, "chats")
    os.makedirs(chats, exist_ok=True)
    lines = [
        json.dumps({"sessionId": session_uuid, "kind": "main"}),
        json.dumps({"id": "m1", "timestamp": "2026-08-13T10:00:01Z", "type": "gemini",
                    "content": "", "model": None,
                    "thoughts": [{"subject": thought_subject, "description": "razonamiento"}]}),
    ]
    with open(os.path.join(chats, fname), "w") as f:
        f.write("\n".join(lines) + "\n")


def _install_fake_home(tmp_path, projects, monkeypatch):
    """Home falso con ~/.gemini/projects.json ({slug: abs_path})."""
    home = tmp_path / "home"
    home.mkdir()
    gdir = home / ".gemini"
    gdir.mkdir(parents=True)
    with open(gdir / "projects.json", "w") as f:
        json.dump({"projects": projects}, f)
    monkeypatch.setenv("HOME", str(home))
    return home


# t4 — discovery desde projects.json (multi-store)
def test_auto_discovery_from_projects_json(tmp_path, monkeypatch):
    home = _install_fake_home(tmp_path, {str(tmp_path / "repo"): "master"}, monkeypatch)
    _write_store(home, "master", "session-2026-08-13T09-59-11111111.jsonl",
                 "11111111-1111-4111-8111-111111111111", "Planning GAP-01")

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = GeminiHarvestSource(ledger_path=ledger)
    assert source.detect() is True
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["gemini"] == 1

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    raws = [e["event"] for e in entries if e["event"]["event_type"] != "SESSION_SUMMARY"]
    assert len(raws) == 1
    assert raws[0]["payload"]["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert raws[0]["payload"]["session_locator"] == "session-2026-08-13T09-59-11111111.jsonl"


# t5 — dos stores cosechados, cursor por store (slug/basename)
def test_auto_discovery_harvests_two_stores(tmp_path, monkeypatch):
    home = _install_fake_home(
        tmp_path, {str(tmp_path / "r1"): "master", str(tmp_path / "r2"): "zombie"}, monkeypatch
    )
    _write_store(home, "master", "session-master.jsonl",
                 "11111111-1111-4111-8111-111111111111", "Planning A")
    _write_store(home, "zombie", "session-zombie.jsonl",
                 "22222222-2222-4222-8222-222222222222", "Planning B")

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = GeminiHarvestSource(ledger_path=ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["gemini"] == 2
    assert h.harvest_all()["gemini"] == 0  # sin duplicados

    with open(config) as f:
        cursors = json.load(f)
    assert set(cursors["agent:gemini"]["files"]) == {
        "master/session-master.jsonl", "zombie/session-zombie.jsonl",
    }


# t6 — colisión de basename entre stores (ambas sesiones se cosechan)
def test_same_basename_in_two_stores_harvested_separately(tmp_path, monkeypatch):
    home = _install_fake_home(
        tmp_path, {str(tmp_path / "r1"): "master", str(tmp_path / "r2"): "zombie"}, monkeypatch
    )
    _write_store(home, "master", "session-shared.jsonl",
                 "11111111-1111-4111-8111-111111111111", "Planning A")
    _write_store(home, "zombie", "session-shared.jsonl",
                 "22222222-2222-4222-8222-222222222222", "Planning B")

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = GeminiHarvestSource(ledger_path=ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["gemini"] == 2  # ambas sesiones, mismo basename
    assert h.harvest_all()["gemini"] == 0

    with open(config) as f:
        cursors = json.load(f)
    assert set(cursors["agent:gemini"]["files"]) == {
        "master/session-shared.jsonl", "zombie/session-shared.jsonl",
    }


# t7a — proyecto sin dir de chats → detect() False
def test_auto_detect_false_without_any_store(tmp_path, monkeypatch):
    home = _install_fake_home(tmp_path, {str(tmp_path / "repo"): "master"}, monkeypatch)
    source = GeminiHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source.detect() is False


# t8 — env CAUSADB_GEMINI_PROJECT_DIR → single-store fallback
def test_auto_discovery_env_project_dir_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))  # sin projects.json → env fallback
    project_dir = _install_fixture(tmp_path)
    monkeypatch.setenv("CAUSADB_GEMINI_PROJECT_DIR", project_dir)
    source = GeminiHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source.detect() is True
    raws = list(source.harvest(None))
    assert len(raws) == 4


# t8b — workspace del ledger con chats/ → single-store fallback
def test_auto_discovery_workspace_chats_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CAUSADB_GEMINI_PROJECT_DIR", raising=False)
    # chats/ en el raíz del workspace del ledger (sin projects.json ni env)
    chats = tmp_path / "chats"
    chats.mkdir()
    lines = _load_fixture_lines()
    (chats / "session-2026-07-02T18-18-0d212d94.jsonl").write_text("\n".join(lines) + "\n")
    ledger = str(tmp_path / ".causadb" / "ledger.log")
    source = GeminiHarvestSource(ledger_path=ledger)
    assert source.detect() is True
    raws = list(source.harvest(None))
    assert len(raws) == 4


# t9 — backward-compat: project_dir explícito → claves de cursor por basename
def test_legacy_project_dir_mode_cursor_keys_are_basenames(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)  # project_dir explícito (fixture)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["gemini"] == 4
    with open(config) as f:
        cursors = json.load(f)
    assert set(cursors["agent:gemini"]["files"]) == {"session-2026-07-02T18-18-0d212d94.jsonl"}


# t10 — migración de cursor legacy → multi-store (sin duplicados)
def test_legacy_cursor_migrated_to_store_scoped_keys(tmp_path, monkeypatch):
    home = _install_fake_home(tmp_path, {str(tmp_path / "repo"): "master"}, monkeypatch)
    chats = os.path.join(str(home), ".gemini", "tmp", "master", "chats")
    os.makedirs(chats, exist_ok=True)
    lines = _load_fixture_lines()
    with open(os.path.join(chats, "session-2026-07-02T18-18-0d212d94.jsonl"), "w") as f:
        f.write("\n".join(lines) + "\n")

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")

    # 1ª corrida en modo legacy (project_dir explícito) → cursor por basename
    legacy = GeminiHarvestSource(
        ledger_path=ledger,
        project_dir=str(home / ".gemini" / "tmp" / "master"),
    )
    h1 = Harvester(ledger, config)
    h1.register_source(legacy)
    assert h1.harvest_all()["gemini"] == 4
    with open(config) as f:
        cursors = json.load(f)
    assert "session-2026-07-02T18-18-0d212d94.jsonl" in cursors["agent:gemini"]["files"]

    # 2ª corrida con la fuente auto-descubierta (multi-store): la clave legacy
    # se migra re-encuadrada por os.path.exists → 0 eventos (sin duplicados)
    auto = GeminiHarvestSource(ledger_path=ledger)
    h2 = Harvester(ledger, config)
    h2.register_source(auto)
    assert h2.harvest_all()["gemini"] == 0, "cursor migrado debe evitar duplicados"

    with open(config) as f:
        cursors = json.load(f)
    files = cursors["agent:gemini"]["files"]
    assert "master/session-2026-07-02T18-18-0d212d94.jsonl" in files
    assert "session-2026-07-02T18-18-0d212d94.jsonl" not in files


# t10b — migración: colisión de basename → clave vieja preservada (warning)
def test_legacy_cursor_migration_collision_keeps_old_key(tmp_path):
    from causadb._harvest_source import migrate_legacy_cursor
    s1 = tmp_path / "s1" / "chats"
    s2 = tmp_path / "s2" / "chats"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    (s1 / "session-x.jsonl").write_text("x\n")
    (s2 / "session-x.jsonl").write_text("x\n")
    cursor = {"files": {"session-x.jsonl": {"offset": 5, "mtime": 1.0}}}
    out = migrate_legacy_cursor(cursor, [str(s1), str(s2)])
    assert "session-x.jsonl" in out["files"], "colisión → preservar clave legacy"
    # ghost (no existe en ningún store) → también se preserva
    cursor2 = {"files": {"ghost.jsonl": {"offset": 3, "mtime": 0.0}}}
    out2 = migrate_legacy_cursor(cursor2, [str(s1)])
    assert "ghost.jsonl" in out2["files"]


def test_legacy_cursor_migration_rehomes_by_exists(tmp_path):
    from causadb._harvest_source import migrate_legacy_cursor
    chats = tmp_path / "master" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-a.jsonl").write_text("a\n")
    cursor = {"files": {"session-a.jsonl": {"offset": 2, "mtime": 7.0}}}
    out = migrate_legacy_cursor(cursor, [str(chats)])
    assert out["files"] == {"master/session-a.jsonl": {"offset": 2, "mtime": 7.0}}


# t11 — projects.json corrupto → fallback fail-open (no crash)
def test_auto_discovery_corrupt_projects_json_fallback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    gdir = home / ".gemini"
    gdir.mkdir()
    (gdir / "projects.json").write_text("{ not json")
    monkeypatch.setenv("HOME", str(home))
    source = GeminiHarvestSource(ledger_path=str(tmp_path / "ledger.log"))
    assert source.detect() is False
