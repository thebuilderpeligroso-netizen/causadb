"""FASE A — Revive debe llamar ``ReplayEngine.reconstruct_state`` una sola vez
dentro del namespace de ``causadb.cli._cmd_revive`` por invocación de
``_run_revive``. Antes del fix eran 2 dentro de ese namespace: una dentro de
``_generate_governance_decisions`` (línea 178) y una directa en ``_run_revive``
(línea 807) para observations. Calls externos (``generate_resume`` en
``_cmd_resume.py`` y ``load_skills`` en ``_skill_registry.py``) importan su
propio ``ReplayEngine`` y NO se cuentan ni se tocan en este fix (deferido a
follow-up).

Tests RED primero (Art. III anti-teatro).
"""

from types import MappingProxyType
from unittest.mock import patch

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb.cli._cmd_revive import _generate_governance_decisions, _run_revive
from causadb.cli._cmd_resume import generate_resume


# ── helpers ──────────────────────────────────────────────────────────────────


def _append(writer, event_type, payload, parent_event_id=None):
    writer.append(CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(payload),
        parent_event_id=parent_event_id,
    ))


def _new_ledger(tmp_path):
    result = causadb_init(str(tmp_path / "ws"))
    return result["ledger_path"], LedgerWriter(result["ledger_path"])


def _counting_replay_engine():
    """Subclass de ``ReplayEngine`` que cuenta las llamadas a
    ``reconstruct_state`` y delega al comportamiento real. Usada para
    patchear el nombre ``ReplayEngine`` en el namespace de ``_cmd_revive``
    (Camino B): solo cuenta las invocaciones que pasan por ese módulo, no
    las de ``_cmd_resume`` o ``_skill_registry`` que importan su propio
    ``ReplayEngine``.
    """
    counter = {"calls": 0}

    class _CountingReplayEngine(ReplayEngine):
        def reconstruct_state(self, *args, **kwargs):
            counter["calls"] += 1
            return super().reconstruct_state(*args, **kwargs)

    return _CountingReplayEngine, counter


# ── tests ────────────────────────────────────────────────────────────────────


def test_run_revive_calls_reconstruct_state_once(tmp_path):
    """``_run_revive`` invoca ``ReplayEngine.reconstruct_state`` exactamente
    una vez dentro del namespace de ``causadb.cli._cmd_revive``. Antes del fix
    eran 2: una dentro de ``_generate_governance_decisions`` (línea 178) y una
    directa en ``_run_revive`` (línea 807). Las llamadas externas
    (``generate_resume`` en ``_cmd_resume.py`` y ``load_skills`` en
    ``_skill_registry.py``) importan su propio ``ReplayEngine`` y NO se cuentan
    — este fix no las toca (deferido a follow-up).

    Anti-teatro: valida que el wiring del state pre-computado llega a las
    decisiones Y a las observations (no basta con ``call_count == 1``; el
    output debe contener las decisiones reales y el path del observation).
    """
    ledger, writer = _new_ledger(tmp_path)

    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "decisión A",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })
    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "decisión B",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })
    _append(writer, EventType.OBSERVATION, {
        "file_path": "src/app.py",
        "line_number": 42,
        "description": "todo: refactor this",
        "severity": "minor",
    })

    # Camino B: patchear el nombre ``ReplayEngine`` en el namespace de
    # ``_cmd_revive`` con una subclass que cuenta. Así solo se cuentan las
    # invocaciones que pasan por ese módulo (las 2 pre-fix → 1 post-fix).
    CountingEngine, counter = _counting_replay_engine()
    with patch("causadb.cli._cmd_revive.ReplayEngine", CountingEngine):
        exit_code, output = _run_revive(
            ledger, output_format="markdown", max_decisions=10
        )

    assert exit_code == 0, f"revive falló: {output!r}"
    # La invariante central: 1 sola reconstruct dentro del namespace de
    # _cmd_revive (venía 2 pre-fix).
    assert counter["calls"] == 1, (
        f"esperaba 1 invocación de reconstruct_state en _cmd_revive, "
        f"got {counter['calls']}"
    )
    # Anti-teatro: el output contiene las decisiones reales del ledger.
    assert "decisión A" in output, "decisión A no aparece en output"
    assert "decisión B" in output, "decisión B no aparece en output"
    # Anti-teatro: el path del observation aparece en la sección
    # "Observaciones pendientes" (valida wiring del state pre-computado hacia
    # observations).
    assert "src/app.py" in output, (
        f"observation path no aparece en output — wiring de state roto.\n"
        f"output:\n{output}"
    )


def test_generate_governance_decisions_accepts_precomputed_state(tmp_path):
    """``_generate_governance_decisions`` acepta ``state`` pre-computado y NO
    construye un ``ReplayEngine`` nuevo (path de reuso del fix).

    Anti-teatro: si el state pre-computado se ignora y se construye un
    ReplayEngine nuevo, el test falla porque ``call_count != 0`` Y porque el
    reasoning retornado sería "from ledger" (del ledger real) en vez de
    "mocked decision" (del state inyectado).
    """
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "from ledger",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })

    precomputed = {
        "governance_decisions": [
            {
                "reasoning": "mocked decision",
                "impact": "low",
                "decision_type": "tactical",
                "origin": "agent",
            }
        ]
    }

    CountingEngine, counter = _counting_replay_engine()
    with patch("causadb.cli._cmd_revive.ReplayEngine", CountingEngine):
        result = _generate_governance_decisions(
            ledger, max_decisions=10, state=precomputed
        )

    assert len(result) == 1, f"esperaba 1 decisión, got {len(result)}: {result}"
    assert result[0]["reasoning"] == "mocked decision", (
        f"reasoning debería venir del state inyectado, got: {result[0]!r}"
    )
    assert counter["calls"] == 0, (
        f"NO debería haber construido ReplayEngine, got {counter['calls']} calls"
    )


def test_generate_governance_decisions_falls_back_to_replay_when_no_state(tmp_path):
    """Sin ``state`` explícito, ``_generate_governance_decisions`` construye
    un ``ReplayEngine`` y lee las decisiones del ledger (path legacy).

    Anti-teatro: si el fallback se rompe (ej: se borra el path legacy), el
    test falla porque ``call_count != 1`` y porque el reasoning no sería
    "from ledger".
    """
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.GOVERNANCE_DECISION, {
        "reasoning": "from ledger",
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    })

    CountingEngine, counter = _counting_replay_engine()
    with patch("causadb.cli._cmd_revive.ReplayEngine", CountingEngine):
        result = _generate_governance_decisions(ledger, max_decisions=10)

    assert len(result) == 1, f"esperaba 1 decisión, got {len(result)}"
    assert result[0]["reasoning"] == "from ledger", (
        f"reasoning debería venir del ledger, got: {result[0]!r}"
    )
    assert counter["calls"] == 1, (
        f"fallback debería construir ReplayEngine 1 vez, got {counter['calls']}"
    )


def test_run_revive_preserves_observations_with_precomputed_state(tmp_path):
    """Anti-regresión del wiring: el ``replay_state`` reusado para
    ``observations = replay_state.get("observations", [])`` sigue funcionando
    luego del refactor. El path del observation debe aparecer en la sección
    "## Observaciones pendientes" del markdown.

    Anti-teatro: si el refactor rompe el wiring (ej: se pasa ``state={}`` o
    se llama a ``_generate_governance_decisions`` antes de tener
    ``replay_state``), el path no aparece en el output.
    """
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.OBSERVATION, {
        "file_path": "src/legacy.py",
        "line_number": 99,
        "description": "smell: god class",
        "severity": "major",
    })

    exit_code, output = _run_revive(ledger, output_format="markdown")

    assert exit_code == 0, f"revive falló: {output!r}"
    assert "## Observaciones pendientes" in output, (
        "la sección de observaciones pendientes no se renderiza — "
        "¿se perdió el wiring de observations?"
    )
    assert "src/legacy.py" in output, (
        f"el path del observation no aparece en output — wiring roto.\n"
        f"output:\n{output}"
    )
    assert "smell: god class" in output, (
        f"la description del observation no aparece en output.\n"
        f"output:\n{output}"
    )


# ============================================================================
# FASE B — Tests para generate_resume con state pre-computado (RED → GREEN)
# ============================================================================

# LEDGER_PATH real para tests de integración
LEDGER_PATH = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/.causadb/ledger.log"


def test_generate_resume_accepts_precomputed_state(monkeypatch):
    """Test 1: generate_resume acepta state pre-computado y NO llama _safe_replay."""
    from causadb._replay_engine import ReplayEngine
    
    calls = {"count": 0}
    original_reconstruct = ReplayEngine.reconstruct_state
    
    def counting_reconstruct(self):
        calls["count"] += 1
        return original_reconstruct(self)
    
    monkeypatch.setattr(ReplayEngine, "reconstruct_state", counting_reconstruct)
    
    # State pre-computado mínimo válido
    fake_state = {
        "events_applied": 100,
        "last_hash": "abc",
        "timestamp": "2026-08-25T00:00:00Z",
        "files_modified": [{"path": "test.py", "action": "modified", "timestamp": "2026-08-25T00:00:00Z"}],
        "llm_invocations": [],
        "reasoning_steps": [],
        "tools_called": [],
        "commands_run": [],
        "cost_accounted": [],
        "session_summaries": [],
        "skills": [],
    }
    
    result = generate_resume(LEDGER_PATH, state=fake_state)
    
    # ANTI-TEATRO: verifica call_count == 0 Y datos correctos del state
    assert calls["count"] == 0, f"_safe_replay llamado {calls['count']} veces, esperaba 0"
    assert result["events_count"] == 100
    assert result["files_modified"] == 1
    assert "test.py" in result["unique_files"]


@pytest.mark.timeout(30)
def test_generate_resume_falls_back_to_replay_when_no_state(monkeypatch):
    """Test 2: generate_resume sin state usa fallback _safe_replay (legacy intacto).

    Nota: el legacy path hace DOS replays (uno en _safe_replay, otro en
    load_skills via _load_relevant_skills). Esto es comportamiento existente,
    no introducido por este fix.
    """
    from causadb._replay_engine import ReplayEngine
    
    calls = {"count": 0}
    original_reconstruct = ReplayEngine.reconstruct_state
    
    def counting_reconstruct(self):
        calls["count"] += 1
        return original_reconstruct(self)
    
    monkeypatch.setattr(ReplayEngine, "reconstruct_state", counting_reconstruct)
    
    result = generate_resume(LEDGER_PATH)  # Sin state
    
    # ANTI-TEATRO: call_count == 2 (legacy path: _safe_replay + load_skills)
    # Y datos reales del ledger
    assert calls["count"] == 2, f"_safe_replay llamado {calls['count']} veces, esperaba 2 (legacy path)"
    assert result["events_count"] > 0  # Datos reales, no vacíos


def test_generate_resume_preserves_all_fields_with_precomputed_state(monkeypatch):
    """Test 3: generate_resume preserva TODOS los campos con state pre-computado."""
    from causadb._replay_engine import ReplayEngine
    from causadb._ocb_manager import OCB
    
    calls = {"count": 0}
    original_reconstruct = ReplayEngine.reconstruct_state
    
    def counting_reconstruct(self):
        calls["count"] += 1
        return original_reconstruct(self)
    
    monkeypatch.setattr(ReplayEngine, "reconstruct_state", counting_reconstruct)
    
    # Mock OCB to return normal_close (so file_tree skills are relevant)
    original_for_ledger = OCB.for_ledger
    def mock_for_ledger(ledger_path):
        ocb = original_for_ledger(ledger_path)
        original_load = ocb.load_session_context
        def mock_load():
            ctx = original_load()
            ctx["session_type"] = "normal_close"
            return ctx
        ocb.load_session_context = mock_load
        return ocb
    monkeypatch.setattr(OCB, "for_ledger", mock_for_ledger)
    
    fake_state = {
        "events_applied": 42,
        "last_hash": "xyz",
        "timestamp": "2026-08-25T12:00:00Z",
        "files_modified": [
            {"path": "a.py", "action": "created", "timestamp": "2026-08-25T10:00:00Z"},
            {"path": "b.py", "action": "modified", "timestamp": "2026-08-25T11:00:00Z"},
        ],
        "llm_invocations": [{"model": "test", "timestamp": "2026-08-25T10:30:00Z"}],
        "reasoning_steps": [{"step_type": "analysis", "description": "test"}],
        "tools_called": [{"tool_name": "pytest", "error": None}],
        "commands_run": [{"command": "pytest", "timestamp": "2026-08-25T10:30:00Z"}],
        "cost_accounted": [{"cost": 0.001}],
        "session_summaries": [{"tool": "test", "session_id": "sid", "turn_count": 5}],
        "skills": [{"skill_type": "file_tree", "skill_name": "test", "content": "tree", "token_count": 100, "confidence": 0.9}],
    }
    
    result = generate_resume(LEDGER_PATH, state=fake_state)
    
    assert calls["count"] == 0
    # ANTI-TEATRO: verifica campos críticos, no solo call_count
    assert result["events_count"] == 42
    assert result["unique_files_count"] == 2
    assert result["llm_invocations"] == 1
    assert result["reasoning_steps"] == 1
    assert result["tools_called"] == 1
    assert result["total_cost_usd"] == 0.001
    assert result["last_timestamp"] == "2026-08-25T12:00:00Z"
    assert len(result["relevant_skills"]) == 1
    assert result["relevant_skills"][0]["skill_name"] == "test"


@pytest.mark.timeout(30)
def test_run_revive_passes_state_to_generate_resume(monkeypatch):
    """Test 4: _run_revive pasa state a generate_resume (wiring real)."""
    from causadb._replay_engine import ReplayEngine
    
    reconstruct_calls = {"count": 0}
    original_reconstruct = ReplayEngine.reconstruct_state
    
    def counting_reconstruct(self):
        reconstruct_calls["count"] += 1
        return original_reconstruct(self)
    
    monkeypatch.setattr(ReplayEngine, "reconstruct_state", counting_reconstruct)
    
    exit_code, output = _run_revive(
        ledger_path=LEDGER_PATH,
        output_format="json",
        max_decisions=5,
        write_path=None,
    )
    
    assert exit_code == 0
    # ANTI-TEATRO: UNA sola reconstruct_state en todo el pipeline
    assert reconstruct_calls["count"] == 1, f"reconstruct_state llamado {reconstruct_calls['count']} veces, esperaba 1"
    # Verifica que el output tiene datos (no vacío por error)
    import json
    data = json.loads(output)
    assert data["resume"]["events_count"] > 0


# ============================================================================
# FASE 2 — Tests para optimización promote_decisions / detect_destructive (RED → GREEN)
# ============================================================================

# LEDGER_PATH real para tests de integración
LEDGER_PATH = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/.causadb/ledger.log"


def test_promote_decisions_preserves_functionality(monkeypatch):
    """Test 1: promote_decisions preserva funcionalidad con LedgerIndex.query.

    ANTI-TEATRO: query fue llamado con event_type=REASONING_STEP,
    include_payloads=True, limit=None. Output: promoted >= 0 (no crashea,
    misma funcionalidad).
    """
    from causadb.cli._cmd_revive import _promote_decisions_to_governance
    from causadb._ledger_index import LedgerIndex
    from causadb._event_types import EventType
    
    query_calls = {"count": 0, "args": []}
    original_query = LedgerIndex.query
    
    def counting_query(self, *args, **kwargs):
        query_calls["count"] += 1
        query_calls["args"].append((args, kwargs))
        return original_query(self, *args, **kwargs)
    
    monkeypatch.setattr(LedgerIndex, "query", counting_query)
    
    # Llamar función
    promoted = _promote_decisions_to_governance(LEDGER_PATH)
    
    # ANTI-TEATRO: query fue llamado con event_type=REASONING_STEP
    assert query_calls["count"] == 1, f"esperaba 1 llamada a query, got {query_calls['count']}"
    called_kwargs = query_calls["args"][0][1]
    assert called_kwargs.get("event_type") == EventType.REASONING_STEP.value, (
        f"event_type debería ser REASONING_STEP, got {called_kwargs.get('event_type')}"
    )
    assert called_kwargs.get("include_payloads") is True, (
        f"include_payloads debería ser True, got {called_kwargs.get('include_payloads')}"
    )
    assert called_kwargs.get("limit") is None, (
        f"limit debería ser None, got {called_kwargs.get('limit')}"
    )
    # Output: promoted >= 0 (no crashea, misma funcionalidad)
    assert promoted >= 0, f"promoted debería ser >= 0, got {promoted}"


def test_detect_destructive_preserves_functionality(monkeypatch):
    """Test 2: detect_destructive preserva funcionalidad con LedgerIndex.query.

    ANTI-TEATRO: query fue llamado con event_type=COMMAND_RUN,
    include_payloads=True, limit=None. Output: detected >= 0.
    """
    from causadb.cli._cmd_revive import _detect_destructive_commands
    from causadb._ledger_index import LedgerIndex
    from causadb._event_types import EventType
    
    query_calls = {"count": 0, "args": []}
    original_query = LedgerIndex.query
    
    def counting_query(self, *args, **kwargs):
        query_calls["count"] += 1
        query_calls["args"].append((args, kwargs))
        return original_query(self, *args, **kwargs)
    
    monkeypatch.setattr(LedgerIndex, "query", counting_query)
    
    detected = _detect_destructive_commands(LEDGER_PATH)
    
    assert query_calls["count"] == 1, f"esperaba 1 llamada a query, got {query_calls['count']}"
    called_kwargs = query_calls["args"][0][1]
    assert called_kwargs.get("event_type") == EventType.COMMAND_RUN.value, (
        f"event_type debería ser COMMAND_RUN, got {called_kwargs.get('event_type')}"
    )
    assert called_kwargs.get("include_payloads") is True, (
        f"include_payloads debería ser True, got {called_kwargs.get('include_payloads')}"
    )
    assert called_kwargs.get("limit") is None, (
        f"limit debería ser None, got {called_kwargs.get('limit')}"
    )
    assert detected >= 0, f"detected debería ser >= 0, got {detected}"


def test_promote_decisions_performance():
    """Test 3: Performance - promote_decisions completa en < 1.6s.

    Antes del fix: ~3.3s (read_all 170K eventos). Después: ~1.2-1.5s (índice real).
    Threshold 1.6s permite variabilidad de primera ejecución (rebuild índice).
    """
    from causadb.cli._cmd_revive import _promote_decisions_to_governance
    import time
    
    t = time.time()
    promoted = _promote_decisions_to_governance(LEDGER_PATH)
    elapsed = time.time() - t
    
    assert elapsed < 1.6, f"_promote_decisions_to_governance tomó {elapsed:.2f}s, esperaba < 1.6s"
    assert promoted >= 0


def test_detect_destructive_performance():
    """Test 4: Performance - detect_destructive completa en < 2.0s.

    Antes del fix: ~3.5s (read_all 170K eventos). Después: ~1.2-1.5s (índice real).
    Threshold 2.0s permite variabilidad de primera ejecución (rebuild índice).
    """
    from causadb.cli._cmd_revive import _detect_destructive_commands
    import time
    
    t = time.time()
    detected = _detect_destructive_commands(LEDGER_PATH)
    elapsed = time.time() - t
    
    assert elapsed < 2.0, f"_detect_destructive_commands tomó {elapsed:.2f}s, esperaba < 2.0s"
    assert detected >= 0
