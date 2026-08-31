"""Tests Fase 12 — StoryBoard persistente (ver Chronicle; docs/design_index.md).

Artículo III: RED first — tests escritos antes de la implementación.
Artículo IX: anti-teatro — los tests verifican comportamiento REAL del
dict (no que pasa trivialmente): orden de turns, unión de user_prompt,
files_touched únicos, decisions filtradas por "decision", errors solo
no-vacíos, None con lista vacía o sin LLM_INVOKED, sanitización del
session_id en el filename, e integración con el Harvester (12.3).
"""

import json
import os
import pytest

from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester


# ===================================================================
# Fixtures de raw events realistas — misma forma que producen las
# puntitas (_agent_transcript.py + markers de cursor __harvest_*)
# ===================================================================

def _raw_events_multi_turn() -> list[dict]:
    """Sesión realista de 2 turnos: user_prompt, reasoning, tool calls
    (uno con error), responses, archivos tocados (con duplicado) y un
    OBSERVATION que el storyboard debe tolerar sin incluirlo."""
    return [
        # -- Turno 1 --------------------------------------------------
        {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:00Z",
         "step_type": "user_prompt", "description": "Implementar el módulo X",
         "__harvest_session_id": "session-abc"},
        {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:01Z",
         "step_type": "analysis", "description": "Analizar el schema actual",
         "__harvest_session_id": "session-abc"},
        {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:02Z",
         "step_type": "decision", "reasoning": "Usar SQLite para el store",
         "__harvest_session_id": "session-abc"},
        {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:03Z",
         "tool_name": "read_file", "arguments": {"path": "db.py"},
         "result": "class DB:", "tool_call_id": "t1",
         "__harvest_session_id": "session-abc"},
        {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:04Z",
         "model": "gemini-2.5", "prompt": "fallback-no-usado",
         "response_tokens": 100, "response_content": "Primera respuesta",
         "__harvest_session_id": "session-abc"},
        # -- Turno 2 --------------------------------------------------
        {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:05Z",
         "step_type": "user_prompt", "description": "Ahora escribe los tests",
         "__harvest_session_id": "session-abc"},
        {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:06Z",
         "step_type": "reflection", "description": "Revisar cobertura",
         "__harvest_session_id": "session-abc"},
        {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:07Z",
         "tool_name": "write_file", "arguments": {"path": "tests/test_db.py"},
         "result": "ok", "error": "", "tool_call_id": "t2",
         "__harvest_session_id": "session-abc"},
        {"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:08Z",
         "tool_name": "bash", "arguments": {"cmd": "pytest"},
         "result": "", "error": "Exit 1: 2 failed", "tool_call_id": "t3",
         "__harvest_session_id": "session-abc"},
        {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:09Z",
         "model": "gemini-2.5", "prompt": "fallback-no-usado",
         "response_tokens": 50, "response_content": "Segunda respuesta",
         "__harvest_session_id": "session-abc"},
        # -- Archivos (con duplicado) + OBSERVATION --------------------
        {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:10Z",
         "path": "db.py", "action": "modify", "__harvest_session_id": "session-abc"},
        {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:11Z",
         "path": "db.py", "action": "modify", "__harvest_session_id": "session-abc"},
        {"type": "FILE_MODIFIED", "timestamp": "2026-08-01T10:00:12Z",
         "path": "tests/test_db.py", "action": "create", "__harvest_session_id": "session-abc"},
        {"type": "OBSERVATION", "timestamp": "2026-08-01T10:00:13Z",
         "file_path": "db.py", "line_number": 3, "description": "sin documentar",
         "severity": "low", "__harvest_session_id": "session-abc"},
    ]


# ===================================================================
# 12.2 — build_storyboard() función pura
# ===================================================================

class TestBuildStoryboard:
    """Tests para build_storyboard(raw_events, tool)."""

    def test_lista_vacia_retorna_none(self):
        """Lista vacía → None (no generar storyboard vacío)."""
        from causadb._storyboard import build_storyboard
        assert build_storyboard([], tool="gemini") is None

    def test_sin_llm_invoked_retorna_none(self):
        """Sin LLM_INVOKED → None (consistente con summarize_session)."""
        from causadb._storyboard import build_storyboard
        raws = [{"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:00Z",
                 "tool_name": "bash", "error": "", "__harvest_session_id": "s1"}]
        assert build_storyboard(raws, tool="gemini") is None

    def test_detalle_completo_multi_turn(self):
        """Verifica contenido EXACTO del dict: orden de turns, unión de
        user_prompt, reasoning por ventana, tool_calls, files únicos,
        decisions, errors, tokens y duration."""
        from causadb._storyboard import build_storyboard
        board = build_storyboard(_raw_events_multi_turn(), tool="gemini")
        assert board is not None

        # Metadatos
        assert board["tool"] == "gemini"
        assert board["session_id"] == "session-abc"
        assert board["turn_count"] == 2
        assert board["created_at"].endswith("Z")  # ISO UTC

        # Turns en orden de aparición (una por LLM_INVOKED)
        assert [t["timestamp"] for t in board["turns"]] == [
            "2026-08-01T10:00:04Z",
            "2026-08-01T10:00:09Z",
        ]
        # user_prompt prioriza sobre prompt del LLM_INVOKED
        assert board["turns"][0]["prompt"] == "Implementar el módulo X"
        assert board["turns"][1]["prompt"] == "Ahora escribe los tests"
        assert board["turns"][0]["assistant_response"] == "Primera respuesta"
        assert board["turns"][1]["assistant_response"] == "Segunda respuesta"
        # reasoning = pasos no-user_prompt de la ventana del turno
        assert board["turns"][0]["reasoning"] == [
            "Analizar el schema actual",
            "Usar SQLite para el store",
        ]
        assert board["turns"][1]["reasoning"] == ["Revisar cobertura"]

        # tool_calls: detalle completo en orden, error solo si presente
        tcs = board["tool_calls"]
        assert len(tcs) == 3
        assert tcs[0]["tool_name"] == "read_file"
        assert tcs[0]["input"] == {"path": "db.py"}
        assert tcs[0]["result"] == "class DB:"
        assert "error" not in tcs[0]  # sin error → clave ausente
        assert tcs[1]["tool_name"] == "write_file"
        assert tcs[1]["input"] == {"path": "tests/test_db.py"}
        assert "error" not in tcs[1]  # error="" → clave ausente
        assert tcs[2]["tool_name"] == "bash"
        assert tcs[2]["error"] == "Exit 1: 2 failed"

        # files_touched únicos (db.py duplicado se colapsa)
        assert board["files_touched"] == ["db.py", "tests/test_db.py"]

        # decisions: solo step_type con "decision"
        assert [d["step_type"] for d in board["decisions"]] == ["decision"]
        assert board["decisions"][0]["reasoning"] == "Usar SQLite para el store"

        # errors: solo TOOL_CALLED con error no vacío
        assert [e["tool_name"] for e in board["errors"]] == ["bash"]
        assert board["errors"][0]["error"] == "Exit 1: 2 failed"

        # tokens_used = suma de response_tokens
        assert board["tokens_used"] == 150

        # duration_s: primer (10:00:00Z) → último timestamp (10:00:13Z)
        assert board["duration_s"] == 13

        # El OBSERVATION no contamina ninguna sección del storyboard
        assert "OBSERVATION" not in str(board)

    def test_fallback_prompt_del_llm(self):
        """Sin REASONING_STEP user_prompt → usa prompt del LLM_INVOKED."""
        from causadb._storyboard import build_storyboard
        raws = [
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
             "prompt": "prompt directo", "response_content": "ok",
             "response_tokens": 5, "__harvest_session_id": "s1"},
        ]
        board = build_storyboard(raws, tool="opencode")
        assert board is not None
        assert board["turns"][0]["prompt"] == "prompt directo"
        assert board["turns"][0]["reasoning"] == []
        assert board["turn_count"] == 1

    def test_session_id_unknown_sin_marker(self):
        """Sin __harvest_session_id → session_id "unknown"."""
        from causadb._storyboard import build_storyboard
        raws = [{"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
                 "prompt": "x", "response_content": "y", "response_tokens": 1}]
        board = build_storyboard(raws, tool="hermes")
        assert board is not None
        assert board["session_id"] == "unknown"

    def test_created_at_parseable_utc(self):
        """created_at es ISO 8601 UTC con Z y parseable."""
        from datetime import datetime, timezone
        from causadb._storyboard import build_storyboard
        raws = [{"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
                 "prompt": "x", "response_content": "y", "response_tokens": 1,
                 "__harvest_session_id": "s1"}]
        board = build_storyboard(raws, tool="gemini")
        created = board["created_at"]
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0  # UTC


# ===================================================================
# Seguridad — sanitización del session_id en el filename
# ===================================================================

class TestSanitizeSessionId:
    """El session_id va en el filename — debe neutralizar path traversal."""

    def test_sanitiza_separadores_de_ruta(self):
        from causadb._storyboard import sanitize_session_id
        out = sanitize_session_id("a/b\\c")
        assert "/" not in out and "\\" not in out
        assert os.path.basename(out) == out  # componente único

    def test_sanitiza_path_traversal(self):
        from causadb._storyboard import sanitize_session_id
        for evil in ("../../etc/passwd", "..", "../..", "..\\..\\win", "a/../b"):
            out = sanitize_session_id(evil)
            assert ".." not in out, f"path traversal no neutralizado: {out!r}"
            assert "/" not in out and "\\" not in out
            assert out != ""

    def test_vacio_retorna_unknown(self):
        from causadb._storyboard import sanitize_session_id
        assert sanitize_session_id("") == "unknown"
        assert sanitize_session_id(None) == "unknown"

    def test_caracteres_peligrosos_reemplazados(self):
        from causadb._storyboard import sanitize_session_id
        out = sanitize_session_id("a b:c?d*e")
        assert all(c.isalnum() or c in "._-" for c in out)


# ===================================================================
# 12.3 — Inyección en el pipeline de harvest
# ===================================================================

class MockAgentSource(HarvestSource):
    """Fuente mock con source_type en _AGENT_SOURCES (patrón Fase 11)."""

    def __init__(self, ledger_path, events=None, detect_result=True):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._detect_result = detect_result

    def source_type(self):
        return "gemini"

    def cursor_key(self):
        return "agent:gemini"

    def detect(self):
        return self._detect_result

    def harvest(self, cursor=None):
        if not self._events:
            return []
        if cursor is None:
            return list(self._events)
        idx = cursor.get("index", 0)
        if idx >= len(self._events):
            return []
        return list(self._events[idx:])

    def advance_cursor(self, cursor, harvested_raw_events):
        old_index = cursor.get("index", 0) if cursor else 0
        return {"index": old_index + len(harvested_raw_events)}


class TestHarvestOneEscribeStoryboard:
    def test_harvest_one_escribe_storyboard(self, tmp_path):
        """Harvest de fuente agente → archivo
        <ledger_dir>/stories/gemini/<session_id>.json con el detalle."""
        from causadb._storyboard import build_storyboard

        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")
        harvester = Harvester(ledger_path, config_path=config_path)

        raws = _raw_events_multi_turn()
        source = MockAgentSource(ledger_path, events=raws)
        harvester.register_source(source)

        results = harvester.harvest_all()
        assert results["gemini"] == len(raws)

        story_file = tmp_path / "stories" / "gemini" / "session-abc.json"
        assert story_file.exists(), f"Storyboard no escrito en {story_file}"

        with open(story_file) as f:
            board = json.load(f)
        assert board["session_id"] == "session-abc"
        assert board["tool"] == "gemini"
        assert board["turn_count"] == 2

        # El archivo persiste el MISMO detalle que la función pura
        expected = build_storyboard(raws, tool="gemini")
        assert board["turns"] == expected["turns"]
        assert board["tool_calls"] == expected["tool_calls"]
        assert board["files_touched"] == expected["files_touched"]

        # Artículo I: el storyboard NO es un evento del ledger. El ledger
        # solo tiene los raw events + el SESSION_SUMMARY (Fase 11).
        with open(ledger_path) as f:
            entries = [json.loads(line) for line in f]
        assert len(entries) == len(raws) + 1
        assert all("storyboard" not in e["event"]["event_type"].lower()
                   for e in entries)

    def test_sin_llm_invoked_no_escribe_storyboard(self, tmp_path):
        """Fuente agente sin LLM_INVOKED → build_storyboard retorna None
        → no se escribe ningún archivo de storyboard."""
        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")
        harvester = Harvester(ledger_path, config_path=config_path)

        raws = [{"type": "TOOL_CALLED", "timestamp": "2026-08-01T10:00:00Z",
                 "tool_name": "bash", "error": "", "__harvest_session_id": "s1"}]
        source = MockAgentSource(ledger_path, events=raws)
        harvester.register_source(source)

        results = harvester.harvest_all()
        assert results["gemini"] == 1

        stories_dir = tmp_path / "stories"
        if stories_dir.exists():
            assert not list(stories_dir.rglob("*.json")), \
                "No debe escribirse storyboard sin LLM_INVOKED"

    def test_sanitiza_session_id_en_filename(self, tmp_path):
        """Session_id con path traversal → el archivo queda DENTRO de
        stories/gemini/, nunca escapa del directorio base."""
        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")
        harvester = Harvester(ledger_path, config_path=config_path)

        raws = [{"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:00Z",
                 "prompt": "x", "response_content": "y", "response_tokens": 1,
                 "__harvest_session_id": "../../evil"}]
        source = MockAgentSource(ledger_path, events=raws)
        harvester.register_source(source)
        harvester.harvest_all()

        # El storyboard quedó dentro de stories/gemini/ y NO se creó
        # ningún archivo fuera (path traversal real fallaría aquí).
        story_dir = tmp_path / "stories" / "gemini"
        assert story_dir.exists()
        json_files = list(story_dir.rglob("*.json"))
        assert len(json_files) == 1
        assert not (tmp_path / "evil").exists(), \
            "Path traversal: se creó un archivo fuera de stories/"

    def test_harvest_one_separa_sesiones_del_batch(self, tmp_path):
        """Un harvest puede abarcar VARIAS sesiones (gemini agrega todos
        los archivos modificados desde el último cursor). Cada sesión debe
        tener su propio archivo con SOLO sus eventos (spec 12.1: un
        archivo por sesión) — ningún turno/tool_call de otra sesión."""
        from causadb._storyboard import build_storyboard

        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")
        harvester = Harvester(ledger_path, config_path=config_path)

        sesion_a = [
            {"type": "REASONING_STEP", "timestamp": "2026-08-01T10:00:00Z",
             "step_type": "user_prompt", "description": "Prompt de A",
             "__harvest_session_id": "session-A"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T10:00:01Z",
             "model": "gemini-2.5", "prompt": "x", "response_tokens": 30,
             "response_content": "respuesta de A", "__harvest_session_id": "session-A"},
        ]
        sesion_b = [
            {"type": "REASONING_STEP", "timestamp": "2026-08-01T11:00:00Z",
             "step_type": "user_prompt", "description": "Prompt de B",
             "__harvest_session_id": "session-B"},
            {"type": "TOOL_CALLED", "timestamp": "2026-08-01T11:00:01Z",
             "tool_name": "bash", "arguments": {"cmd": "pytest"},
             "result": "ok", "__harvest_session_id": "session-B"},
            {"type": "LLM_INVOKED", "timestamp": "2026-08-01T11:00:02Z",
             "model": "gemini-2.5", "prompt": "y", "response_tokens": 60,
             "response_content": "respuesta de B", "__harvest_session_id": "session-B"},
        ]
        raws = sesion_a + sesion_b
        source = MockAgentSource(ledger_path, events=raws)
        harvester.register_source(source)

        results = harvester.harvest_all()
        assert results["gemini"] == len(raws)

        story_dir = tmp_path / "stories" / "gemini"
        archivos = sorted(p.name for p in story_dir.iterdir())
        assert archivos == ["session-A.json", "session-B.json"], archivos

        # Cada archivo contiene SOLO su sesión: sin mezcla de turnos,
        # tool_calls, prompts ni tokens de la otra sesión.
        with open(story_dir / "session-A.json") as f:
            board_a = json.load(f)
        with open(story_dir / "session-B.json") as f:
            board_b = json.load(f)

        assert board_a["turn_count"] == 1
        assert board_a["turns"][0]["prompt"] == "Prompt de A"
        assert board_a["tokens_used"] == 30
        assert board_a["tool_calls"] == []

        assert board_b["turn_count"] == 1
        assert board_b["turns"][0]["prompt"] == "Prompt de B"
        assert board_b["tokens_used"] == 60
        assert len(board_b["tool_calls"]) == 1
        assert board_b["tool_calls"][0]["tool_name"] == "bash"

        # Los storyboards coinciden con la función pura por sesión
        expected_a = build_storyboard(sesion_a, tool="gemini")
        expected_b = build_storyboard(sesion_b, tool="gemini")
        assert board_a["turns"] == expected_a["turns"]
        assert board_b["turns"] == expected_b["turns"]

    def test_fallo_storyboard_no_rompe_harvest(self, tmp_path, monkeypatch):
        """Si build_storyboard lanza una excepción, el harvest continúa
        (try/except degradante, 12.3) y el ledger sí se escribe."""
        ledger_path = str(tmp_path / "ledger.log")
        config_path = str(tmp_path / ".harvester_cursors.json")
        harvester = Harvester(ledger_path, config_path=config_path)

        raws = _raw_events_multi_turn()
        source = MockAgentSource(ledger_path, events=raws)
        harvester.register_source(source)

        def _boom(*args, **kwargs):
            raise RuntimeError("storyboard explosion")

        monkeypatch.setattr("causadb._storyboard.build_storyboard", _boom)

        results = harvester.harvest_all()
        assert results["gemini"] == len(raws)

        # El ledger se escribió completo (eventos + SESSION_SUMMARY)
        with open(ledger_path) as f:
            entries = [json.loads(line) for line in f]
        assert len(entries) == len(raws) + 1


# ===================================================================
# Config — storyboard_path (Fase 12.1)
# ===================================================================

class TestConfigStoryboardPath:
    def test_default_storyboard_path(self):
        """Default: <ledger_dir>/stories."""
        from causadb._config import CausaDBConfig
        cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
        assert cfg.storyboard_path == "/tmp/causadb/stories"

    def test_storyboard_path_override(self):
        """Override explícito se respeta."""
        from causadb._config import CausaDBConfig
        cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log",
                            storyboard_path="/tmp/other/stories")
        assert cfg.storyboard_path == "/tmp/other/stories"

    def test_storyboard_path_from_env(self, monkeypatch):
        """CAUSADB_STORYBOARD_PATH se lee en from_env."""
        from causadb._config import CausaDBConfig
        monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/env/ledger.log")
        monkeypatch.setenv("CAUSADB_STORYBOARD_PATH", "/tmp/env/storyboards")
        cfg = CausaDBConfig.from_env()
        assert cfg.storyboard_path == "/tmp/env/storyboards"

    def test_storyboard_path_default_from_env(self, monkeypatch):
        """Sin env → from_env computa <ledger_dir>/stories."""
        from causadb._config import CausaDBConfig
        monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/env/ledger.log")
        monkeypatch.delenv("CAUSADB_STORYBOARD_PATH", raising=False)
        cfg = CausaDBConfig.from_env()
        assert cfg.storyboard_path == "/tmp/env/stories"
