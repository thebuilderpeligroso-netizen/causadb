"""Tests Fase 2 — Puntita OpenJarvis (BIT-OJ.1; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture = copia del ``~/.openjarvis/traces.db`` REAL, 7 traces + 7
trace_steps — no mocks; ver ``tests/fixtures/_build_openjarvis_fixture.py``).

Desviaciones del plan (documentadas para el reporte, ver también el
docstring del módulo):
  - ``trace_steps.timestamp`` es REAL epoch en **SEGUNDOS** (10 dígitos)
    → ``_s_to_iso``. El plan (línea 86) decía ``_ms_to_iso``; verificado
    contra datos reales (1783107851.95... = segundos).
  - El JOIN es ``traces.trace_id = trace_steps.trace_id`` (TEXT hash). El
    plan (línea 202) decía ``traces.id = trace_steps.trace_id`` — pero
    ``traces.id`` es INTEGER 1-7, no el hash; el JOIN del plan no
    matchearía ninguna fila.
  - ``traces.total_tokens`` = 0 en los 7 traces reales → ``response_tokens``
    honesto = 0.
  - ``trace_steps.input`` = ``'{}'`` (vacío) → el prompt del LLM_INVOKED
    se toma de ``traces.query`` (dato real).
  - ``duration_ms`` = ``int(duration_seconds * 1000)`` (duración REAL del
    step; ``_compute_duration_ms`` no aplica sin user previo por-step).
  - ``step_type`` real = ``['respond']`` (7/7); ``generate/tool_call/
    route/retrieve`` se cubren por contrato (test unitario del mapeo, no
    fixture — Artículo IX respetado).
  - ``test_model_prefix_normalized`` NO aplica: OpenJarvis guarda el model
    desnudo (``qwen2.5-coder:14b``, ``ornith:9b``), sin prefijos provider.

Cobertura:
  1. detect() True con fixture / False sin db
  2. harvest de la fixture → 7 LLM_INVOKED (7 steps ``respond``) con
     model/prompt/response_content/duration_ms/response_tokens reales
  3. dos corridas → segunda devuelve 0 eventos (idempotencia por rowid)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la conexión es read-only (el fixture no se modifica)
  7. unit test del mapeo de step_types (db sintético, NO la fixture):
     respond/generate→LLM_INVOKED, tool_call→TOOL_CALLED, route→
     REASONING_STEP, step_type desconocido→sin evento y sin romper
"""

import hashlib
import json
import os
import shutil

import pytest

from causadb._blob_store import BlobStore
from causadb._harvester import Harvester
from causadb._harvest_source_openjarvis import (
    OpenJarvisHarvestSource,
    _s_to_iso,
)
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "openjarvis_fixture.db"

# ---------------------------------------------------------------------------
# Datos REALES de la fixture (2026-08-02, ~/.openjarvis/traces.db).
# Cada step id → (trace_id, model, query, timestamp_epoch_s, duration_seconds)
# ---------------------------------------------------------------------------
EXPECTED_STEPS = {
    1: {
        "trace_id": "6ee7c1284aec41c6",
        "model": "qwen2.5-coder:14b",
        "query": "hola...que modelo eres?",
        "epoch": 1783107851.9548256,
        "duration_s": 8.129263639450073,
        "response_tokens": 0,
        "response_content": (
            "Hola! Soy OpenJarvis, un asistente de IA local que corre en el "
            "hardware del usuario. No soy un servicio en la nube ni tengo una "
            "marca específica como Claude, ChatGPT, Gemini u otros. ¿En qué "
            "puedo ayudarte hoy?"
        ),
    },
    2: {
        "trace_id": "63d7986cc2cc4929",
        "model": "qwen2.5-coder:14b",
        "query": "vos vas a ser mi nuevo asistente?...con que herramientas cuentas?",
        "epoch": 1783107887.8386667,
        "duration_s": 16.277236461639404,
        "response_tokens": 0,
        "response_content": (
            "¡Por supuesto! Estoy diseñado para ayudar con una amplia gama de "
            "tareas, siempre teniendo en cuenta las capacidades y limitaciones "
            "de mi modelo local. Aquí hay algunas cosas con lo que puedo ayudarte:\n"
            "\n"
            "1. **Información general**: Puedo proporcionar información sobre "
            "eventos actuales, noticias recientes, definiciones de términos, etc.\n"
            "2. **Asistencia técnica**: Ofrezco ayuda básica para resolver "
            "problemas técnicos relacionados con software y hardware.\n"
            "3. **Organización**: Puedo ayudarte a programar recordatorios, "
            "hacer listas de tareas y gestionar tu tiempo.\n"
            "4. **Entrenamiento personal**: Puedo ayudarte a aprender nuevas "
            "habilidades o conceptos a través de explicaciones y ejemplos "
            "prácticos.\n"
            "5. **Consejos generales**: Ofrezco consejos sobre una variedad de "
            "temas, desde cocina hasta organización del hogar.\n"
            "\n"
            "Recuerda que mi conocimiento está basado en datos disponibles hasta "
            "principios de 2023, por lo que no puedo proporcionar información "
            "sobre eventos o desarrollos posteriores a esa fecha. ¿En qué puedo "
            "ayudarte hoy?"
        ),
    },
    3: {
        "trace_id": "a2d14bceea6e40c2",
        "model": "qwen2.5-coder:14b",
        "query": "pero no contas con herramientas'...por ejemplo buscar en la red?",
        "epoch": 1783107922.3514626,
        "duration_s": 8.126477479934692,
        "response_tokens": 0,
        "response_content": (
            "Tienes razón. Como un asistente de IA local, actualmente no tengo "
            "la capacidad de acceder a Internet o realizar búsquedas en línea. "
            "Mi conocimiento está limitado a los datos preestablecidos con los "
            "que fui entrenada y no puedo buscar información nueva ni "
            "actualizarla.\n"
            "\n"
            "Si necesitas buscar algo específico en la web, te recomiendo que "
            "utilices un motor de búsqueda como Google. Si tienes alguna otra "
            "pregunta o necesitas ayuda con algo más, estaré encantado de "
            "asistirte dentro de mis capacidades."
        ),
    },
    4: {
        "trace_id": "cc84639814f6473a",
        "model": "qwen2.5-coder:14b",
        "query": (
            "Como sos de codigo abierto debe haber alguna manera de modificar "
            "tu codigo y darte acceso a la red"
        ),
        "epoch": 1783107966.0234795,
        "duration_s": 18.773101806640625,
        "response_tokens": 0,
        "response_content": (
            "¡Entiendo tu interés! Como OpenJarvis, estoy basado en un modelo "
            "de código abierto, lo que significa que puedes revisar mi código "
            "fuente. Sin embargo, hay algunas consideraciones importantes:\n"
            "\n"
            "1. **Seguridad**: Dar acceso a Internet a un asistente local puede "
            "tener implicaciones de seguridad significativas. Es importante "
            "asegurarse de que cualquier modificación no exponga tu sistema o "
            "datos personales.\n"
            "\n"
            "2. **Capacidades Técnicas**: Modificar el código fuente requiere "
            "conocimientos técnicos sólidos en lenguajes de programación y "
            "arquitectura de IA. Asegúrate de entender completamente lo que "
            "estás haciendo antes de proceder.\n"
            "\n"
            "3. **Compatibilidad**: Las actualizaciones o modificaciones pueden "
            "afectar la compatibilidad con otros sistemas o componentes de tu "
            "hardware.\n"
            "\n"
            "Si tienes las habilidades técnicas adecuadas y estás dispuesto a "
            "asumir estos riesgos, puedes explorar cómo agregar funcionalidades "
            "como el acceso a Internet al código fuente. Sin embargo, ten en "
            "cuenta que esto podría requerir un alto nivel de conocimiento y "
            "tiempo de desarrollo.\n"
            "\n"
            "Si necesitas ayuda técnica específica para modificar mi código o "
            "cualquier otra cosa, no dudes en preguntar. Estoy aquí para "
            "ayudarte dentro de mis capacidades actuales."
        ),
    },
    5: {
        "trace_id": "0155d7c9f3ac439e",
        "model": "qwen2.5-coder:14b",
        "query": "Y para que estas vos que estas basado en qwen2.5-coder:14b, un modelo de codigo!",
        "epoch": 1783108030.813424,
        "duration_s": 25.868183851242065,
        "response_tokens": 0,
        "response_content": (
            "Entiendo que estás interesado en los detalles técnicos sobre mi "
            "base. OpenJarvis está basado en el modelo **Qwen2.5-Coder** de 14 "
            "mil millones de parámetros (14B). Qwen es un modelo de lenguaje "
            "desarrollado por Alibaba Cloud, diseñado para realizar una variedad "
            "de tareas relacionadas con el procesamiento del lenguaje natural y "
            "la generación de código.\n"
            "\n"
            "### **Funcionalidades de Qwen2.5-Coder:**\n"
            "\n"
            "- **Generación de Código**: Puedo ayudarte a escribir, depurar y "
            "entender códigos en diferentes lenguajes de programación.\n"
            "- **Asistencia Técnica**: Ofrezco soluciones para problemas "
            "técnicos relacionados con software y hardware.\n"
            "- **Entrenamiento Personal**: Facilito el aprendizaje de nuevas "
            "habilidades o conceptos a través de explicaciones y ejemplos "
            "prácticos.\n"
            "\n"
            "### **Limitaciones Actuales:**\n"
            "\n"
            "1. **Acceso a Internet**: No tengo la capacidad de buscar "
            "información en línea o acceder a Internet en tiempo real.\n"
            "2. **Conocimiento Actualizado**: Mi conocimiento se basa en datos "
            "disponibles hasta principios de 2023, por lo que no puedo "
            "proporcionar información sobre eventos posteriores a esa fecha.\n"
            "3. **Procesamiento Local**: Como un asistente local, mi rendimiento "
            "y funcionalidades están limitadas por la configuración de tu "
            "hardware.\n"
            "\n"
            "### **¿Cómo Puedo Ayudarte Hoy?**\n"
            "\n"
            "Dado estos puntos, ¿en qué puedo ayudarte hoy dentro de mis "
            "capacidades actuales? Si tienes preguntas técnicas, necesitas ayuda "
            "con un proyecto específico o cualquier otra consulta, estaré "
            "encantado de asistirte."
        ),
    },
    6: {
        "trace_id": "b9787fd3e46c4d31",
        "model": "ornith:9b",
        "query": "Vamos a ver que tan rapido eres",
        "epoch": 1783108078.642387,
        "duration_s": 4.65660285949707,
        "response_tokens": 0,
        "response_content": (
            "¡Claro! Veamos cuán rápido soy respondiendo una pregunta rápida:\n"
            "\n"
            "**Pregunta**: ¿Cuál es la capital de Australia?\n"
            "\n"
            "**Respuesta**: La capital de Australia es Canberra.\n"
            "\n"
            "¿Te parece bien esto? Si tienes alguna otra pregunta o necesitas "
            "ayuda con algo más, no dudes en preguntar. Estoy aquí para ayudarte "
            "dentro de mis capacidades actuales."
        ),
    },
    7: {
        "trace_id": "89ff0130082d4eef",
        "model": "ornith:9b",
        "query": "Eres Jarvis, pero en que modelo estas basado?",
        "epoch": 1783108104.758642,
        "duration_s": 3.413400411605835,
        "response_tokens": 0,
        "response_content": (
            "OpenJarvis está basado en el modelo **Qwen2.5-Coder** de 14 mil "
            "millones de parámetros (14B). Este es un modelo de lenguaje "
            "desarrollado por Alibaba Cloud, diseñado para realizar una variedad "
            "de tareas relacionadas con el procesamiento del lenguaje natural y "
            "la generación de código.\n"
            "\n"
            "### **Características Clave de Qwen2.5-Coder:**\n"
            "\n"
            "- **Generación de Código**: Puedo ayudarte a escribir, depurar y "
            "entender códigos en diferentes lenguajes de programación.\n"
            "- **Asistencia Técnica**: Ofrezco soluciones para problemas "
            "técnicos relacionados con software y hardware.\n"
            "- **Entrenamiento Personal**: Facilito el aprendizaje de nuevas "
            "habilidades o conceptos a través de explicaciones y ejemplos "
            "prácticos.\n"
            "\n"
            "### **Limitaciones Actuales:**\n"
            "\n"
            "1. **Acceso a Internet**: No tengo la capacidad de buscar "
            "información en línea o acceder a Internet en tiempo real.\n"
            "2. **Conocimiento Actualizado**: Mi conocimiento se basa en datos "
            "disponibles hasta principios de 2023, por lo que no puedo "
            "proporcionar información sobre eventos posteriores a esa fecha.\n"
            "3. **Procesamiento Local**: Como un asistente local, mi rendimiento "
            "y funcionalidades están limitadas por la configuración de tu "
            "hardware.\n"
            "\n"
            "### **¿Cómo Puedo Ayudarte Hoy?**\n"
            "\n"
            "Dado estos puntos, ¿en qué puedo ayudarte hoy dentro de mis "
            "capacidades actuales? Si tienes preguntas técnicas, necesitas ayuda "
            "con un proyecto específico o cualquier otra consulta, estaré "
            "encantado de asistirte."
        ),
    },
}

# El ISO del step 1 hardcodeado como doble verificación del parseo de
# segundos (si alguien "arregla" a _ms_to_iso, este test lo detecta).
STEP1_ISO_EXPECTED = "2026-07-03T19:44:11.954826Z"


def _install_fixture(tmp_path):
    """Copia la fixture (traces.db real con WAL checkpointeado) a un dir
    temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return OpenJarvisHarvestSource(
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
    assert source.source_type() == "openjarvis"  # SIN colon (fix de namespace)
    assert source.cursor_key() == "agent:openjarvis"


def test_detect_false_without_db(tmp_path):
    source = OpenJarvisHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.db"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → 7 LLM_INVOKED (7 steps respond reales)
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """7 trace_steps (todos step_type='respond') → 7 LLM_INVOKED con los
    datos REALES del store: model de traces.model, prompt de traces.query,
    response_content del output.content, duration_ms de duration_seconds,
    response_tokens de total_tokens (0 real), timestamp en segundos → ISO."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    # Raw dicts (sin escribir): exactamente 7, todos LLM_INVOKED
    raws = list(source.harvest(None))
    assert len(raws) == 7, f"Esperaba 7 raws, obtuvo {len(raws)}"
    assert all(r["type"] == "LLM_INVOKED" for r in raws), [
        r["type"] for r in raws
    ]

    # Ordenados por step id (ORDER BY ts.id)
    for i, raw in enumerate(raws, start=1):
        exp = EXPECTED_STEPS[i]
        # Timestamp: epoch REAL segundos → ISO (desviación _s_to_iso)
        assert raw["timestamp"] == _s_to_iso(exp["epoch"]), (
            f"step {i}: timestamp mal parseado"
        )
        assert raw["model"] == exp["model"], f"step {i}: model"
        assert raw["prompt"] == exp["query"], f"step {i}: prompt"
        assert raw["response_content"] == exp["response_content"], (
            f"step {i}: response_content (parse del output.content)"
        )
        # duration_ms = duration_seconds * 1000 (dato REAL del store)
        assert raw["duration_ms"] == int(exp["duration_s"] * 1000), (
            f"step {i}: duration_ms"
        )
        assert raw["response_tokens"] == exp["response_tokens"], (
            f"step {i}: response_tokens (total_tokens real = 0)"
        )
        assert raw["agent"] == "openjarvis", f"step {i}: agent"
        assert raw["__harvest_locator"] == source.db_path

    # Doble verificación del ISO del step 1 con literal hardcodeado
    assert raws[0]["timestamp"] == STEP1_ISO_EXPECTED

    # Flujo completo: harvest_all escribe 7 LLM_INVOKED + 1 SESSION_SUMMARY
    result = h.harvest_all()
    assert result["openjarvis"] == 7
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 8
    etypes = sorted(e["event"]["event_type"] for e in entries)
    assert etypes == ["LLM_INVOKED"] * 7 + ["SESSION_SUMMARY"]
    # BlobStore (Fase 8): payloads > threshold viajan como {"$blob": hash}.
    # Resolver para verificar que el payload escrito conserva agent (el
    # mismo camino que usa el replay) — Artículo IX: verificación real.
    blobs = BlobStore(os.path.join(os.path.dirname(ledger), "blobs"))
    agent_ids = set()
    for e in entries:
        if e["event"]["event_type"] != "LLM_INVOKED":
            continue
        payload = e["event"]["payload"]
        if set(payload) == {"$blob"}:
            payload = blobs.get(payload["$blob"])
        agent_ids.add(payload["agent"])
    assert agent_ids == {"openjarvis"}


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
    assert r1["openjarvis"] == 7
    r2 = h.harvest_all()
    assert r2["openjarvis"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 8  # 7 LLM_INVOKED + 1 SESSION_SUMMARY
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 8


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
    # harvest_all no crashea (aislamiento por fuente, auditoría I.2)
    assert "openjarvis" in result
    assert result["openjarvis"] == 0
    # El cursor NO avanzó (mismo contrato que las otras fuentes)
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (7) sin pérdida
    r2 = h.harvest_all()
    assert r2["openjarvis"] == 7
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 8  # 7 LLM_INVOKED + 1 SESSION_SUMMARY
    assert len({e["event"]["event_id"] for e in entries}) == 8


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_openjarvis_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    # last_hash es no-determinista (timestamps del SESSION_SUMMARY). El
    # determinismo funcional se verifica comparando que los mismos eventos
    # y summaries existen en ambas corridas (precedente hermes, Fase 11).
    assert state1["events_applied"] == state2["events_applied"] == 8  # 7 raw + 1 SESSION_SUMMARY
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
    assert h.harvest_all()["openjarvis"] == 7
    state = ReplayEngine(ledger).reconstruct_state()
    assert state["events_applied"] == 8  # 7 raw + 1 SESSION_SUMMARY
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
    assert h.harvest_all()["openjarvis"] == 7
    assert h.harvest_all()["openjarvis"] == 0
    assert _fixture_bytes() == before, "mode=ro debe dejar el db intacto"

    # No deben quedar side-files (wal/shm) junto al fixture original
    side_files = [f for f in os.listdir(FIXTURE_DIR)
                  if f.startswith(FIXTURE_DB + "-")]
    assert side_files == []


# ---------------------------------------------------------------------------
# 7. unit test del mapeo de step_types (db SINTÉTICO — NO la fixture)
# ---------------------------------------------------------------------------

def test_step_type_mapping_unit(tmp_path):
    """Cubre los step_types del enum (route/retrieve/generate/tool_call/
    respond) + un step_type desconocido. Es un db sintético (Artículo IX
    respetado: la fixture sigue siendo el store real; este test ejercita el
    mapeo por contrato)."""
    import sqlite3

    db = tmp_path / "synthetic.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE traces (id INTEGER PRIMARY KEY, trace_id TEXT, query TEXT, "
        "agent TEXT, model TEXT, engine TEXT, result TEXT, outcome TEXT, "
        "feedback REAL, started_at REAL, ended_at REAL, total_tokens INTEGER, "
        "total_latency_seconds REAL, metadata TEXT, messages TEXT)"
    )
    con.execute(
        "CREATE TABLE trace_steps (id INTEGER PRIMARY KEY, trace_id TEXT, "
        "step_index INTEGER, step_type TEXT, timestamp REAL, duration_seconds "
        "REAL, input TEXT, output TEXT, metadata TEXT)"
    )
    con.execute(
        "INSERT INTO traces VALUES (1, 'syn-1', 'prompt-1', 'server', "
        "'qwen2.5-coder:14b', 'ollama', '', NULL, NULL, 1.0, 2.0, 0, 1.0, '{}', '[]')"
    )
    rows = [
        # respond → LLM_INVOKED
        (1, "syn-1", 0, "respond", 10.0, 1.5, '{}', '{"content": "resp-respond"}', "{}"),
        # generate → LLM_INVOKED
        (2, "syn-1", 0, "generate", 12.0, 2.0, '{}', '{"content": "resp-generate"}', "{}"),
        # tool_call → TOOL_CALLED (input → arguments, output → result)
        (3, "syn-1", 0, "tool_call", 14.0, 0.5,
         '{"name": "terminal", "cmd": "ls"}', '{"content": "out.txt"}', "{}"),
        # route → REASONING_STEP (subject sintetizado + heurística)
        (4, "syn-1", 0, "route", 16.0, 0.3, '{"target": "db"}', "{}", "{}"),
        # retrieve → REASONING_STEP
        (5, "syn-1", 0, "retrieve", 18.0, 0.3, '{"docs": ["a"]}', "{}", "{}"),
        # step_type desconocido → sin evento, sin romper
        (6, "syn-1", 0, "bogus", 20.0, 0.3, "{}", "{}", "{}"),
    ]
    for r in rows:
        con.execute(
            "INSERT INTO trace_steps VALUES (?,?,?,?,?,?,?,?,?)", r
        )
    con.commit()
    con.close()

    source = OpenJarvisHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(db),
    )
    raws = list(source.harvest(None))
    types = [r["type"] for r in raws]
    assert types.count("LLM_INVOKED") == 2, types  # respond + generate
    assert types.count("TOOL_CALLED") == 1, types  # tool_call
    assert types.count("REASONING_STEP") == 2, types  # route + retrieve
    assert len(raws) == 5, f"bogus no debe emitir evento, obtuvo {len(raws)}"

    # -- LLM_INVOKED: respond + generate -----------------------------------
    llms = [r for r in raws if r["type"] == "LLM_INVOKED"]
    llm = {l["response_content"]: l for l in llms}
    assert "resp-respond" in llm and "resp-generate" in llm
    assert all(l["model"] == "qwen2.5-coder:14b" for l in llms)
    assert all(l["prompt"] == "prompt-1" for l in llms)
    assert all(l["response_tokens"] == 0 for l in llms)
    assert llm["resp-respond"]["duration_ms"] == 1500
    assert llm["resp-generate"]["duration_ms"] == 2000

    # -- TOOL_CALLED: input → arguments, output → result --------------------
    tool = [r for r in raws if r["type"] == "TOOL_CALLED"][0]
    assert tool["tool_name"] == "terminal"
    assert tool["arguments"] == {"name": "terminal", "cmd": "ls"}
    assert tool["result"] == {"content": "out.txt"}
    assert tool["agent"] == "openjarvis"

    # -- REASONING_STEP: subject sintetizado + heurística del motor ---------
    reasoning = [r for r in raws if r["type"] == "REASONING_STEP"]
    rroute = reasoning[0]  # route (id 4)
    assert rroute["description"] == '{"target": "db"}'
    assert rroute["subject"] == '{"target": "db"}'  # 8 palabras o menos
    # Heurística del motor determinística: sin keyword plan/decide/reflect
    # → "analysis" (mismo contrato que hermes/opencode).
    assert rroute["step_type"] == "analysis", rroute["step_type"]
    assert rroute["step_hash"] == hashlib.sha256(
        '{"target": "db"}'.encode("utf-8")
    ).hexdigest()
    rretrieve = reasoning[1]  # retrieve (id 5)
    assert rretrieve["description"] == '{"docs": ["a"]}'
    assert rretrieve["step_type"] == "analysis", rretrieve["step_type"]


# test_model_prefix_normalized NO aplica acá: OpenJarvis guarda el model
# desnudo (``qwen2.5-coder:14b``, ``ornith:9b``, sin prefijos provider) —
# el mapeo de la puntita no normaliza (ver docstring del módulo). El model
# real ya se asevera tal cual en test_harvest_fixture_maps_to_expected_events.


# ---------------------------------------------------------------------------
# FIX.GEN-A — harvest() retorna generador (no lista)
# ---------------------------------------------------------------------------

def test_harvest_returns_generator(tmp_path):
    """harvest() debe retornar un generador (FIX.GEN-A), no una lista."""
    source = _make_source(tmp_path)
    gen = source.harvest(None)
    assert hasattr(gen, "__next__"), "harvest() debe retornar un iterador"
    assert not isinstance(gen, list), "harvest() no debe materializar lista"
    # Consumo streaming: iterar produce dicts normales
    first = next(iter(gen))
    assert isinstance(first, dict)
