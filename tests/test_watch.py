"""Tests for F.11.4 — Comando unificado `causadb watch`.

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import json
import os
import socket
import tempfile
import time
from types import MappingProxyType
from unittest.mock import patch, MagicMock

import pytest

from causadb.cli.main import main
from causadb.cli._cmd_watch import _watch_stop, _auto_distill, _start_daemon_service
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# watch start — integration (delegates to vigilante + mcp-proxy)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_inits_services(watch_cleanup):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--no-serve"])
            assert rc == 0
            rc_stop = main(["watch", "stop"])
            assert rc_stop == 0
        finally:
            os.chdir(cwd)


def test_watch_start_no_project_errors():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc = main(["watch", "start", "--no-serve"])
            assert rc != 0
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# watch stop
# ---------------------------------------------------------------------------


def test_watch_stop_without_start():
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc = main(["watch", "stop"])
            assert rc == 0
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# watch status
# ---------------------------------------------------------------------------


def test_watch_status_returns_dict():
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "status"])
            assert rc == 0
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# watch with --ledger override
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_with_explicit_ledger(watch_cleanup):
    with tempfile.TemporaryDirectory() as tmp:
        rc_init = main(["init", tmp])
        assert rc_init == 0
        ledger = os.path.join(tmp, ".causadb", "ledger.log")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc = main(["watch", "start", "--ledger", ledger, "--no-serve"])
            assert rc == 0
            rc = main(["watch", "stop", "--ledger", ledger])
            assert rc == 0
        finally:
            os.chdir(cwd)


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_daemon(watch_cleanup):
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--daemon", "--no-serve"])
            assert rc == 0
            rc = main(["watch", "stop"])
            assert rc == 0
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_watch_non_existent_action():
    """Argparse rejects invalid actions with SystemExit(2)."""
    import sys
    try:
        main(["watch", "nonexistent"])
        assert False, "should have raised"
    except SystemExit as e:
        assert e.code == 2


# ---------------------------------------------------------------------------
# P.2 — Proxy server integration into watch
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_no_proxy_skips_proxy(capsys, watch_cleanup):
    """`watch start --no-proxy` should not start the proxy server."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()  # discard init output
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--no-proxy", "--no-serve"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out.strip().split("\n")[-1])
            assert result.get("proxy_server") == "skipped"
            rc_stop = main(["watch", "stop"])
            assert rc_stop == 0
        finally:
            os.chdir(cwd)


def test_watch_status_includes_proxy_server(capsys):
    """`watch status` should include proxy_server in watch_forks."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()  # discard init output
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "status"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out.strip().split("\n")[-1])
            assert "watch_forks" in result
            assert "proxy_server" in result["watch_forks"]
            assert result["watch_forks"]["proxy_server"] in (
                "running", "stopped", "skipped_by_unit"
            )
        finally:
            os.chdir(cwd)


def test_proxy_server_subcommand_exists():
    """`causadb proxy-server --help` should show start/stop actions."""
    import sys
    try:
        main(["proxy-server", "--help"])
        assert False, "should have raised SystemExit"
    except SystemExit as e:
        assert e.code == 0


def test_proxy_server_help_shows_start_stop():
    """`proxy-server --help` should show start and stop in the help text."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    out = io.StringIO()
    err = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            main(["proxy-server", "--help"])
        assert False, "should have raised SystemExit"
    except SystemExit:
        pass
    help_text = out.getvalue() + err.getvalue()
    assert "start" in help_text
    assert "stop" in help_text


# ---------------------------------------------------------------------------
# R.2 — Auto RESUME.md on watch start
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_first_run_no_resume(capsys, watch_cleanup):
    """`watch start` on fresh workspace → resume session_type is first_run."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()
        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--no-serve"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out)
            assert result.get("resume", {}).get("session_type") == "first_run"
            main(["watch", "stop"])
        finally:
            os.chdir(cwd)


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_abrupt_close_writes_resume_md(capsys, watch_cleanup):
    """`watch start` after abrupt_close → RESUME.md is written to disk."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()

        # Simulate abrupt_close: create OCB_ACTIVE.log without OCB_SUMMARY.json
        ocb_dir = os.path.join(project, ".causadb", "ocb")
        os.makedirs(ocb_dir, exist_ok=True)
        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{"event_type": "FILE_MODIFIED", "ctx_id": "test"}\n')

        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--no-serve"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out)
            assert result.get("resume", {}).get("session_type") == "abrupt_close"
            resume_path = result.get("resume", {}).get("resume_md_path", "")
            assert resume_path != ""
            assert os.path.exists(resume_path)
            md = open(resume_path).read()
            assert "abrupt_close" in md
            main(["watch", "stop"])
        finally:
            os.chdir(cwd)


@pytest.mark.timeout(90)
@pytest.mark.integration
def test_watch_start_normal_close_writes_resume_md(capsys, watch_cleanup):
    """`watch start` after normal_close → RESUME.md is also written.

    F2 (M1) gap residual 4: normal_close requiere session file (evidencia
    de cierre limpio), no solo summary huérfano."""
    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()

        # Simulate normal_close: create OCB_SUMMARY.json + OCB_SESSION_*.log
        ocb_dir = os.path.join(project, ".causadb", "ocb")
        os.makedirs(ocb_dir, exist_ok=True)
        summary_path = os.path.join(ocb_dir, "OCB_SUMMARY.json")
        with open(summary_path, "w") as f:
            json.dump({"sedimentada": True, "work_done": "tests passed"}, f)
        # Evidencia de cierre limpio: session file
        session_path = os.path.join(ocb_dir, "OCB_SESSION_1234567890.log")
        with open(session_path, "w") as f:
            f.write('{"event_id": "x", "event_type": "FILE_MODIFIED"}\n')

        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["watch", "start", "--no-serve"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out)
            assert result.get("resume", {}).get("session_type") == "normal_close"
            resume_path = result.get("resume", {}).get("resume_md_path", "")
            assert resume_path != ""
            assert os.path.exists(resume_path)
            main(["watch", "stop"])
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# F.13.4.4 — Auto-Distill on Watch Stop
# ---------------------------------------------------------------------------


def _make_ledger_with_n_events(tmp_path, n_events):
    """Construye un ledger con ``n_events`` eventos FILE_MODIFIED distintos.

    Cada evento toca un path único (``file_0.py``, ``file_1.py``, ...) para
    que ``distill`` produzca un skill ``file_tree`` con contenido real.

    Retorna el path absoluto del ledger.
    """
    from causadb._config import CausaDBConfig

    ws = tmp_path / "ws"
    os.makedirs(ws, exist_ok=True)
    chronicle = ws / "CAUSADB_CHRONICLE.md"
    chronicle.write_text("# CAUSADB_CHRONICLE.md\n")
    ledger = str(ws / "ledger.log")
    config = CausaDBConfig(ledger_path=ledger)
    writer = LedgerWriter(ledger, config)

    # Genesis (sequence_number=1 con LedgerWriter).
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT, ctx_id="genesis",
        source="causadb:test", source_type="human",
        payload={"action": "init"},
    )
    writer.append(genesis)

    for i in range(n_events):
        payload = {"path": f"src/file_{i}.py", "action": "modify"}
        event = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="test",
            source="causadb:test", source_type="agent",
            payload=MappingProxyType(payload),
        )
        writer.append(event)
    return ledger


def _count_skill_created_events(ledger_path):
    """Cuenta eventos SKILL_CREATED en el ledger via replay."""
    from causadb._replay_engine import ReplayEngine
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    return len(state.get("skills", []))


def test_watch_stop_runs_distill_above_threshold(tmp_path):
    """F.13.4.4 #1 — ledger con 60 eventos → watch stop produce SKILL_CREATED."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    # distill corrió (no skipped por umbral).
    assert result["distill"]["status"] == "ok"
    assert result["distill"]["skills_produced"] > 0
    assert isinstance(result["distill"]["skill_ids"], list)
    assert len(result["distill"]["skill_ids"]) > 0

    # Los skills quedaron persistidos en el ledger como SKILL_CREATED.
    n_skills = _count_skill_created_events(ledger)
    assert n_skills > 0, "SKILL_CREATED events deben aparecer en el ledger"


def test_watch_stop_skips_distill_below_threshold(tmp_path):
    """F.13.4.4 #2 — ledger con 30 eventos (< 50) → distill skipped por umbral."""
    ledger = _make_ledger_with_n_events(tmp_path, 30)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    # distill se skippeó por umbral, no por error.
    assert result["distill"]["status"] == "skipped"
    assert "distill_min_events" in result["distill"]["reason"]
    assert "50" in result["distill"]["reason"]

    # No se registraron skills.
    n_skills = _count_skill_created_events(ledger)
    assert n_skills == 0


def test_watch_stop_distill_degradation_on_error(tmp_path, monkeypatch):
    """F.13.4.4 #3 — distill levanta excepción → watch stop retorna 0 (degradación suave)."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    # Mockear distill para que levante excepción.
    import causadb._distill as distill_mod

    def _boom(_ledger_path, _config=None):
        raise RuntimeError("simulated distill failure")

    monkeypatch.setattr(distill_mod, "distill", _boom)

    rc, out = _watch_stop(ledger)
    assert rc == 0, "watch stop NO debe romperse si distill falla"
    result = json.loads(out)

    assert result["distill"]["status"] == "skipped"
    assert "simulated distill failure" in result["distill"]["reason"]

    # Los daemons igual se intentaron matar (keys presentes).
    assert "vigilante_stopped" in result
    assert "mcp_proxy_stopped" in result
    assert "proxy_server_stopped" in result


def test_watch_stop_distill_produces_skill_ids(tmp_path):
    """F.13.4.4 #4 — watch stop con 60 eventos → skill_ids es lista no vacía."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    skill_ids = result["distill"]["skill_ids"]
    assert isinstance(skill_ids, list)
    assert len(skill_ids) > 0
    # Cada skill_id debe ser un string no vacío (UUID).
    for sid in skill_ids:
        assert isinstance(sid, str)
        assert len(sid) > 0


# ---------------------------------------------------------------------------
# F.13.4.4 — Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_watch_stop_skips_distill_completely(tmp_path, monkeypatch):
    """F.13.4.4 #5 (anti-teatro) — si _auto_distill siempre skippea,
    el test #1 (SKILL_CREATED en ledger) debe fallar.

    Verificamos la propiedad inversa: mutar ``_auto_distill`` para que
    retorne siempre ``{"status": "skipped"}`` → NO aparecen
    SKILL_CREATED en el ledger. Si la implementación real fuera teatro
    (hardcodear skill_ids sin persistir), este test pasaría pero el #1
    fallaría — por eso ambos deben estar presentes.
    """
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    # Mutar _auto_distill para que siempre skippee (simula implementación
    # que no persiste nada al ledger).
    monkeypatch.setattr(
        "causadb.cli._cmd_watch._auto_distill",
        lambda _ledger: {"status": "skipped", "reason": "teatro"},
    )

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)
    assert result["distill"]["status"] == "skipped"

    # Con _auto_distill mutado a skip, NO debe haber SKILL_CREATED.
    n_skills = _count_skill_created_events(ledger)
    assert n_skills == 0, (
        "Si _auto_distill skippea, no debe haber SKILL_CREATED en el ledger. "
        "Si los hay, la implementación está hardcodeando skill_ids sin "
        "persistirlos (teatro — Artículo IX)."
    )


# ---------------------------------------------------------------------------
# F.13.3.5 — Auto-Score on Watch Stop
# ---------------------------------------------------------------------------


def _count_score_recorded_events(ledger_path):
    """Cuenta eventos SCORE_RECORDED en el ledger via replay."""
    from causadb._replay_engine import ReplayEngine
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    return len(state.get("scores_recorded", []))


def test_watch_stop_runs_score_after_distill(tmp_path):
    """F.13.3.5 #1 — ledger con 60 eventos → watch stop produce SCORE_RECORDED."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    # score corrió (no skipped).
    assert result["score"]["status"] == "ok"
    assert "overall_score" in result["score"]

    # El SCORE_RECORDED quedó persistido en el ledger.
    n_scores = _count_score_recorded_events(ledger)
    assert n_scores == 1, (
        "Debe haber exactamente 1 SCORE_RECORDED en el ledger después de "
        "watch stop. Si hay 0, el score no se persistió (teatro — Artículo IX)."
    )


def test_watch_stop_score_degradation_on_error(tmp_path, monkeypatch):
    """F.13.3.5 #2 — compute_score levanta excepción → watch stop retorna 0
    (degradación suave), results["score"]["status"] == "skipped"."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    # Mockear compute_score para que levante excepción.
    import causadb._score as score_mod

    def _boom(_ledger_path, _config=None):
        raise RuntimeError("simulated score failure")

    monkeypatch.setattr(score_mod, "compute_score", _boom)

    rc, out = _watch_stop(ledger)
    assert rc == 0, "watch stop NO debe romperse si score falla"
    result = json.loads(out)

    assert result["score"]["status"] == "skipped"
    assert "simulated score failure" in result["score"]["reason"]

    # Los daemons igual se intentaron matar (keys presentes).
    assert "vigilante_stopped" in result
    assert "mcp_proxy_stopped" in result
    assert "proxy_server_stopped" in result

    # No se persistió ningún SCORE_RECORDED.
    n_scores = _count_score_recorded_events(ledger)
    assert n_scores == 0


def test_watch_stop_score_score_event_id_in_results(tmp_path):
    """F.13.3.5 #3 — watch stop → results["score"]["score_event_id"] presente."""
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    score_block = result["score"]
    assert score_block["status"] == "ok"
    assert "score_event_id" in score_block
    score_event_id = score_block["score_event_id"]
    assert isinstance(score_event_id, str)
    assert len(score_event_id) > 0

    # El event_id reportado debe matchear el que quedó en el ledger.
    from causadb._replay_engine import ReplayEngine
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    scores = state.get("scores_recorded", [])
    assert len(scores) == 1
    assert scores[0]["event_id"] == score_event_id


# ---------------------------------------------------------------------------
# F.13.3.5 — Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_watch_stop_score_skipped_completely(tmp_path, monkeypatch):
    """F.13.3.5 #4 (anti-teatro) — si _auto_score siempre skippea,
    el test #1 (SCORE_RECORDED en ledger) debe fallar.

    Verificamos la propiedad inversa: mutar ``_auto_score`` para que
    retorne siempre ``{"status": "skipped"}`` → NO aparecen
    SCORE_RECORDED en el ledger. Si la implementación real fuera teatro
    (hardcodear score_event_id sin persistir el evento), este test
    pasaría pero el #1 fallaría — por eso ambos deben estar presentes.
    """
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    # Mutar _auto_score para que siempre skippee (simula implementación
    # que no persiste nada al ledger).
    monkeypatch.setattr(
        "causadb.cli._cmd_watch._auto_score",
        lambda _ledger: {"status": "skipped", "reason": "teatro"},
    )

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)
    assert result["score"]["status"] == "skipped"

    # Con _auto_score mutada a skip, NO debe haber SCORE_RECORDED.
    n_scores = _count_score_recorded_events(ledger)
    assert n_scores == 0, (
        "Si _auto_score skippea, no debe haber SCORE_RECORDED en el ledger. "
        "Si los hay, la implementación está hardcodeando score_event_id sin "
        "persistirlo (teatro — Artículo IX)."
    )


# ---------------------------------------------------------------------------
# F.13.3.5 — Test de orden de ejecución (distill ANTES que score)
# ---------------------------------------------------------------------------


def test_watch_stop_runs_distill_before_score(tmp_path):
    """F.13.3.5 #5 — el SKILL_CREATED (del distill) tiene timestamp
    ANTERIOR al SCORE_RECORDED (del score), porque el distill corre
    primero (orden crítico: daemon stop → distill → score).

    Verificado via replay timestamps: el timestamp del último
    SKILL_CREATED debe ser <= al timestamp del SCORE_RECORDED.
    """
    ledger = _make_ledger_with_n_events(tmp_path, 60)

    rc, out = _watch_stop(ledger)
    assert rc == 0
    result = json.loads(out)

    # Ambos hooks deben haber corrido ok.
    assert result["distill"]["status"] == "ok"
    assert result["score"]["status"] == "ok"

    from causadb._replay_engine import ReplayEngine
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()

    skills = state.get("skills", [])
    scores = state.get("scores_recorded", [])

    # Distill con 60 eventos produce al menos 1 skill.
    assert len(skills) >= 1, "distill debe producir skills con 60 eventos"
    # Score produce exactamente 1 SCORE_RECORDED.
    assert len(scores) == 1

    # El timestamp del último SKILL_CREATED debe ser ANTERIOR (<=) al
    # del SCORE_RECORDED. Como los timestamps tienen resolución de
    # microsegundos y distill corre antes que score, en la práctica
    # será estrictamente menor, pero usamos <= para tolerar colisiones
    # de reloj en CI.
    last_skill_ts = skills[-1]["timestamp"]
    score_ts = scores[0]["timestamp"]
    assert last_skill_ts is not None
    assert score_ts is not None
    assert last_skill_ts <= score_ts, (
        f"El SKILL_CREATED (distill) debe tener timestamp ANTERIOR al "
        f"SCORE_RECORDED (score). Got skill={last_skill_ts!r} > "
        f"score={score_ts!r}. Esto viola el orden crítico del roadmap "
        f"13.3.5: daemon stop → distill → score."
    )


# ---------------------------------------------------------------------------
# H-OPS.1 Fase 3 — Fix watch start polling (bug: wait() + sleep(0.3) no espera al daemon real)
# ---------------------------------------------------------------------------


def _make_mock_daemon(is_running_func):
    """Helper to create a mock daemon instance."""
    class MockDaemon:
        is_running = staticmethod(is_running_func)
        daemonize = staticmethod(lambda name: None)
        kill = staticmethod(lambda name, timeout=5.0: True)
    return MockDaemon()


def _make_mock_proc(wait_return=0):
    """Helper to create a mock Popen instance."""
    class MockProc:
        def wait(self, timeout=None):
            return wait_return
        def poll(self):
            return 0
    return MockProc()


def test_watch_start_polling_handles_delayed_is_running(capsys):
    """RED: _watch_start debe pollerear is_running() hasta que el daemon esté listo.

    El bug actual: wait() + sleep(0.3) + check único → falla si el daemon
    tarda >0.3s en escribir PID file. Este test mockea is_running para que
    retorne False→False→True y verifica que el polling lo maneje.

    Con código actual (wait + sleep + check único): FALLA (reporta "failed")
    Con fix (polling activo): PASA (reporta "started")
    """
    call_count = {"count": 0}

    def mock_is_running(name):
        call_count["count"] += 1
        # First 2 calls: False (daemon not ready yet)
        # 3rd call: True (daemon ready)
        if call_count["count"] <= 2:
            return False
        return True

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(mock_is_running)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    ledger = os.path.join(project, ".causadb", "ledger.log")

                    from causadb._workspace import resolve_ledger
                    ledger_path = resolve_ledger(ledger)

                    from causadb.cli._cmd_watch import _watch_start
                    rc, out = _watch_start(ledger_path, no_proxy=True, no_serve=True)
                    assert rc == 0
                    result = json.loads(out)

                    assert result["vigilante"] == "started", (
                        f"Expected vigilante=started with polling, got {result['vigilante']}. "
                        f"is_running was called {call_count['count']} times."
                    )


def test_watch_start_reaps_parent_process():
    """RED: _watch_start debe reapear el proceso padre (Popen.wait) antes de pollerear.

    Evita zombies: el padre del double-fork sale con os._exit(0) y debe ser
    reapeado. Si no se hace wait(), queda zombie.
    """
    wait_called = {"called": False}

    def mock_wait(timeout=None):
        wait_called["called"] = True
        return 0

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(lambda name: True)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=type("MockProc", (), {"wait": staticmethod(mock_wait), "poll": staticmethod(lambda self: 0)})()):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    ledger = os.path.join(project, ".causadb", "ledger.log")

                    from causadb._workspace import resolve_ledger
                    from causadb.cli._cmd_watch import _watch_start
                    ledger_path = resolve_ledger(ledger)

                    rc, out = _watch_start(ledger_path, no_proxy=True, no_serve=True)
                    assert rc == 0

                    assert wait_called["called"], "Must call Popen.wait() to reap parent process"


def test_watch_start_timeout_logs_warning(capsys):
    """RED: Si is_running() no retorna True en timeout, debe loggear WARNING y marcar failed.

    Degradación suave (Artículo V): no crashea, reporta failed con razón.
    """
    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(lambda name: False)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            # Mock time to control timeout
            import time
            time_values = [0.0]

            def mock_time():
                return time_values[0]

            def mock_sleep(seconds):
                time_values[0] += seconds

            with patch("causadb.cli._cmd_watch.time.time", mock_time):
                with patch("causadb.cli._cmd_watch.time.sleep", mock_sleep):
                    with tempfile.TemporaryDirectory() as tmp:
                        project = os.path.join(tmp, "proj")
                        os.makedirs(project)
                        rc_init = main(["init", project])
                        assert rc_init == 0
                        ledger = os.path.join(project, ".causadb", "ledger.log")

                        from causadb._workspace import resolve_ledger
                        from causadb.cli._cmd_watch import _watch_start
                        ledger_path = resolve_ledger(ledger)

                        rc, out = _watch_start(ledger_path, no_proxy=True, no_serve=True)
                        assert rc == 0
                        result = json.loads(out)

                        # Should report failed (not crash)
                        assert result["vigilante"] == "failed"


def test_watch_start_status_shows_all_running(capsys):
    """RED: watch start → watch status debe mostrar todos los servicios running.

    Test de integración: arranca watch, verifica status, para watch.
    Con bug actual: status muestra false para servicios que fallaron en start.
    Con fix: status muestra true para todos.
    """
    from causadb._systemd_utils import SystemdUnitStatus

    call_phase = {"phase": "start"}

    def mock_is_running(name):
        if call_phase["phase"] == "start":
            return False
        return True

    fake_unit_status = SystemdUnitStatus(
        installed=False,
        active=False,
        state="not-found",
        enabled="unknown",
        main_pid=None,
        exec_start="",
        since=None,
        load_error="no unit file in test",
    )

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(mock_is_running)):
        with patch("causadb.cli._cmd_watch.get_unit_status", return_value=fake_unit_status):
            with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
                time_values = [0.0]
                def mock_time():
                    return time_values[0]
                def mock_sleep(seconds):
                    time_values[0] += seconds
                with patch("causadb.cli._cmd_watch.time.time", mock_time):
                    with patch("causadb.cli._cmd_watch.time.sleep", mock_sleep):
                        with tempfile.TemporaryDirectory() as tmp:
                            project = os.path.join(tmp, "proj")
                            os.makedirs(project)
                            rc_init = main(["init", project])
                            assert rc_init == 0
                            capsys.readouterr()

                            cwd = os.getcwd()
                            os.chdir(project)
                            try:
                                rc = main(["watch", "start", "--no-serve"])
                                assert rc == 0

                            # Switch phase to "status" - daemons should now appear running
                                call_phase["phase"] = "status"
                                rc = main(["watch", "status"])
                                assert rc == 0
                                out = capsys.readouterr().out
                                result = json.loads(out.strip().split("\n")[-1])

                            # New format: check watch_forks dict with status strings
                                assert "watch_forks" in result
                                wf = result["watch_forks"]
                                assert wf["vigilante"] == "running"
                                assert wf["mcp_proxy"] == "running"
                                assert wf["proxy_server"] == "running"
                                assert wf["harvest"] == "running"
                                assert wf["serve"] == "running"

                                rc = main(["watch", "stop"])
                                assert rc == 0
                            finally:
                                os.chdir(cwd)


def test_restart_reports_all_started(capsys):
    """RED: causadb restart debe reportar todos los servicios como started.

    Con bug actual: restart → stop ok, start → todos failed.
    Con fix: restart → restart ok, start → todos started.
    """
    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(lambda name: True)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    capsys.readouterr()

                    cwd = os.getcwd()
                    os.chdir(project)
                    try:
                        # --no-systemd: este test pinnea el flujo LEGACY.
                        # Sin el flag, en máquinas con el unit instalado
                        # (Fase U1) cmd_restart gobernaría systemctl real.
                        rc = main(["restart", "--no-serve", "--no-proxy",
                                   "--no-systemd"])
                        assert rc == 0
                        out = capsys.readouterr().out
                        result = json.loads(out.strip().split("\n")[-1])

                        assert result["restart"] == "ok"
                        # All services should be started (except those skipped by flags)
                        assert result["start"]["vigilante"] == "started"
                        assert result["start"]["mcp_proxy"] == "started"
                        # proxy_server and serve skipped due to --no-proxy --no-serve flags
                        assert result["start"]["proxy_server"] == "skipped"
                        assert result["start"]["harvest"] == "started"
                        assert result["start"]["serve"] == "skipped"
                    finally:
                        os.chdir(cwd)


def test_anti_teatro_wait_without_polling_fails():
    """ANTI-TEATRO: Si la implementación usa wait()+sleep()+single_check (bug actual),
    este test FALLA. Si usa polling activo, PASA.

    Verificamos que el código NO sea teatro (hardcodear "started" sin chequear).
    """
    check_count = {"count": 0}

    def mock_is_running(name):
        check_count["count"] += 1
        if check_count["count"] == 1:
            return False  # first check: not ready
        return True  # subsequent: ready

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(mock_is_running)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    ledger = os.path.join(project, ".causadb", "ledger.log")

                    from causadb._workspace import resolve_ledger
                    from causadb.cli._cmd_watch import _watch_start
                    ledger_path = resolve_ledger(ledger)

                    rc, out = _watch_start(ledger_path, no_proxy=True, no_serve=True)
                    assert rc == 0
                    result = json.loads(out)

                    # With polling: should have checked multiple times and gotten "started"
                    # With bug (single check): would get "failed"
                    assert result["vigilante"] == "started", (
                        f"Anti-teatro: implementation must poll until ready. "
                        f"Got vigilante={result['vigilante']}, is_running called {check_count['count']} times."
                    )
                    # Must have called is_running more than once (polling)
                    assert check_count["count"] > 1, (
                        f"Must poll is_running multiple times. Called only {check_count['count']} times."
                    )


# ---------------------------------------------------------------------------
# H-OPS.1 Fase 3 — serve already_running detection (port 7457 check)
# ---------------------------------------------------------------------------


def test_watch_start_serve_already_running_via_port(monkeypatch):
    """RED: _watch_start debe detectar serve ya corriendo via puerto 7457.

    Si el puerto 7457 está ocupado (systemd/legacy), debe reportar
    "already_running" sin intentar arrancar otro serve.
    """
    # Mock socket to say port 7457 is occupied
    original_connect_ex = socket.socket.connect_ex

    def mock_connect_ex(self, address):
        if address == ("127.0.0.1", 7457):
            return 0  # port occupied
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect_ex", mock_connect_ex)

    # Mock is_running to return False (no PID file)
    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(lambda name: False)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            time_values = [0.0]
            def mock_time():
                return time_values[0]
            def mock_sleep(seconds):
                time_values[0] += seconds
            with patch("causadb.cli._cmd_watch.time.time", mock_time):
                with patch("causadb.cli._cmd_watch.time.sleep", mock_sleep):
                    with tempfile.TemporaryDirectory() as tmp:
                        project = os.path.join(tmp, "proj")
                        os.makedirs(project)
                        rc_init = main(["init", project])
                        assert rc_init == 0
                        ledger = os.path.join(project, ".causadb", "ledger.log")

                        from causadb._workspace import resolve_ledger
                        from causadb.cli._cmd_watch import _watch_start
                        ledger_path = resolve_ledger(ledger)

                        rc, out = _watch_start(ledger_path, no_proxy=True)
                        assert rc == 0
                        result = json.loads(out)

                    # Should detect serve already running via port
                        assert result["serve"] == "already_running", (
                            f"Expected serve=already_running when port 7457 occupied, got {result['serve']}"
                        )


def test_watch_start_serve_starts_when_port_free(monkeypatch):
    """RED: _watch_start debe arrancar serve cuando puerto 7457 está libre.

    Si el puerto 7457 está libre, debe arrancar serve normalmente (via polling).
    """
    # Mock socket to say port 7457 is FREE
    def mock_connect_ex(self, address):
        if address == ("127.0.0.1", 7457):
            return 1  # port free (connection refused)
        return 1  # other ports also free

    monkeypatch.setattr(socket.socket, "connect_ex", mock_connect_ex)

    # Mock is_running to return False initially, then True (polling)
    call_count = {"count": 0}

    def mock_is_running(name):
        if name == "serve":
            call_count["count"] += 1
            if call_count["count"] <= 2:
                return False
            return True
        return True  # other services ready

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(mock_is_running)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    ledger = os.path.join(project, ".causadb", "ledger.log")

                    from causadb._workspace import resolve_ledger
                    from causadb.cli._cmd_watch import _watch_start
                    ledger_path = resolve_ledger(ledger)

                    rc, out = _watch_start(ledger_path, no_proxy=True)
                    assert rc == 0
                    result = json.loads(out)

                    # Should start serve normally (polling until ready)
                    assert result["serve"] == "started", (
                        f"Expected serve=started when port free, got {result['serve']}"
                    )


def test_anti_teatro_serve_port_check_not_hardcoded(monkeypatch):
    """ANTI-TEATRO (BIT-CHR.105): el veredicto de serve sigue al puerto real.

    Escenario: puerto 7457 LIBRE en el pre-check -> spawn de serve ->
    puerto pasa a OCUPADO (el serve recién nacido lo bindea) ->
    veredicto debe ser "started".
    Mata mutantes: (a) hardcode de already_running sin consultar puerto,
    (b) no consultar el puerto en absoluto,
    (c) confundir ocupación post-spawn con already_running.
    """
    state = {"serve_spawned": False}
    port_checks = []  # registra estado del mundo en cada consulta a 7457

    def mock_connect_ex(self, address):
        if address == ("127.0.0.1", 7457):
            port_checks.append(state["serve_spawned"])
            return 0 if state["serve_spawned"] else 1  # ocupado solo post-spawn
        return 1

    def fake_popen(cmd, *args, **kwargs):
        # Flip SOLO para serve: el pre-check corre DESPUÉS de los spawns de
        # vigilante/harvest; un flip global mentiría "ocupado" en el pre-check.
        if isinstance(cmd, (list, tuple)) and any(
            str(part).endswith("serve") or str(part) == "serve" for part in cmd
        ):
            state["serve_spawned"] = True
        return _make_mock_proc()

    def mock_is_running(name):
        if name != "serve":
            return True  # resto listo al primer poll (test rápido)
        return state["serve_spawned"]  # predicado de estado, NO índice

    monkeypatch.setattr(socket.socket, "connect_ex", mock_connect_ex)

    with patch("causadb.cli._cmd_watch.get_daemon",
               return_value=_make_mock_daemon(mock_is_running)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", side_effect=fake_popen):
            with patch("causadb.cli._cmd_watch.time.sleep", lambda x: None):
                with tempfile.TemporaryDirectory() as tmp:
                    project = os.path.join(tmp, "proj")
                    os.makedirs(project)
                    rc_init = main(["init", project])
                    assert rc_init == 0
                    ledger = os.path.join(project, ".causadb", "ledger.log")

                    from causadb._workspace import resolve_ledger
                    from causadb.cli._cmd_watch import _watch_start
                    ledger_path = resolve_ledger(ledger)

                    rc, out = _watch_start(ledger_path, no_proxy=True)
                    assert rc == 0
                    result = json.loads(out)

                    # ANTI-TEATRO NÚCLEO: hubo observación PRE-spawn que dijo LIBRE
                    assert False in port_checks, (
                        "La decisión de serve nunca consultó el puerto pre-spawn "
                        f"(observaciones: {port_checks})"
                    )
                    # VEREDICTO: con puerto libre observado, debe arrancar
                    assert result["serve"] == "started", (
                        f"Puerto libre pre-spawn debe producir 'started', "
                        f"no '{result['serve']}'"
                    )


def test_serve_port_check_called(monkeypatch):
    """RED: _is_serve_port_occupied debe ser llamado durante _watch_start.

    Verifica que el chequeo de puerto realmente se ejecuta.
    """
    connect_called = {"count": 0, "addresses": []}

    def mock_connect_ex(self, address):
        if address == ("127.0.0.1", 7457):
            connect_called["count"] += 1
            connect_called["addresses"].append(address)
            return 1  # free
        return 1

    monkeypatch.setattr(socket.socket, "connect_ex", mock_connect_ex)

    with patch("causadb.cli._cmd_watch.get_daemon", return_value=_make_mock_daemon(lambda name: False)):
        with patch("causadb.cli._cmd_watch.subprocess.Popen", return_value=_make_mock_proc()):
            time_values = [0.0]
            def mock_time():
                return time_values[0]
            def mock_sleep(seconds):
                time_values[0] += seconds
            with patch("causadb.cli._cmd_watch.time.time", mock_time):
                with patch("causadb.cli._cmd_watch.time.sleep", mock_sleep):
                    with tempfile.TemporaryDirectory() as tmp:
                        project = os.path.join(tmp, "proj")
                        os.makedirs(project)
                        rc_init = main(["init", project])
                        assert rc_init == 0
                        ledger = os.path.join(project, ".causadb", "ledger.log")

                        from causadb._workspace import resolve_ledger
                        from causadb.cli._cmd_watch import _watch_start
                        ledger_path = resolve_ledger(ledger)

                        rc, out = _watch_start(ledger_path, no_proxy=True)
                        assert rc == 0

                    # Must have checked port 7457 at least once
                        assert connect_called["count"] >= 1, (
                            f"_is_serve_port_occupied must check port 7457. Called {connect_called['count']} times."
                        )
                        assert ("127.0.0.1", 7457) in connect_called["addresses"]


# ---------------------------------------------------------------------------
# Source install detection and PYTHONPATH fix for _start_daemon_service
# ---------------------------------------------------------------------------


def test_is_source_install_source_run():
    """Source run: causadb outside site-packages → True"""
    with patch('causadb.__file__', '/home/user/project/causadb/cli/__init__.py'):
        with patch('site.getsitepackages', return_value=['/usr/lib/python3.12/site-packages']):
            with patch('site.getusersitepackages', return_value='/home/user/.local/lib/python3.12/site-packages'):
                from causadb.cli._cmd_watch import _is_source_install
                assert _is_source_install() == True


def test_is_source_install_pip_install():
    """Pip install: causadb inside site-packages → False"""
    with patch('causadb.__file__', '/usr/lib/python3.12/site-packages/causadb/cli/__init__.py'):
        with patch('site.getsitepackages', return_value=['/usr/lib/python3.12/site-packages']):
            with patch('site.getusersitepackages', return_value='/home/user/.local/lib/python3.12/site-packages'):
                from causadb.cli._cmd_watch import _is_source_install
                assert _is_source_install() == False


def test_is_source_install_editable_install():
    """Editable install: __file__ in site-packages via .pth → False"""
    with patch('causadb.__file__', '/home/user/.local/lib/python3.12/site-packages/causadb/cli/__init__.py'):
        with patch('site.getsitepackages', return_value=['/home/user/.local/lib/python3.12/site-packages']):
            with patch('site.getusersitepackages', return_value='/home/user/.local/lib/python3.12/site-packages'):
                from causadb.cli._cmd_watch import _is_source_install
                assert _is_source_install() == False


def test_is_source_install_venv():
    """Venv install: causadb in venv site-packages → False"""
    with patch('causadb.__file__', '/home/user/venv/lib/python3.12/site-packages/causadb/cli/__init__.py'):
        with patch('site.getsitepackages', return_value=['/home/user/venv/lib/python3.12/site-packages']):
            with patch('site.getusersitepackages', return_value=''):
                from causadb.cli._cmd_watch import _is_source_install
                assert _is_source_install() == False


def test_start_daemon_service_sets_pythonpath_for_source():
    """Verify PYTHONPATH is set to package parent when source install"""
    with patch('causadb.__file__', '/home/user/project/causadb/cli/__init__.py'):
        with patch('causadb.cli._cmd_watch._is_source_install', return_value=True):
            with patch('causadb.cli._cmd_watch.get_daemon') as mock_daemon:
                mock_daemon.return_value.is_running.return_value = True
                with patch('subprocess.Popen') as mock_popen:
                    mock_proc = MagicMock()
                    mock_proc.wait.return_value = 0
                    mock_popen.return_value = mock_proc

                    _start_daemon_service("/path/to/ledger.log", "test_service", {}, ["test", "args"])

                    # Verify Popen called with env containing PYTHONPATH pointing to package parent
                    call_args = mock_popen.call_args
                    env = call_args.kwargs['env']
                    assert 'PYTHONPATH' in env
                    # Package parent should be /home/user/project/causadb (go up 2 levels from causadb/cli/__init__.py)
                    assert env['PYTHONPATH'] == '/home/user/project/causadb'


def test_start_daemon_service_polls_is_running():
    """Verify active polling of is_running(), not hardcoded success"""
    with patch('causadb.cli._cmd_watch._is_source_install', return_value=False):
        with patch('causadb.cli._cmd_watch.get_daemon') as mock_daemon:
            # First 2 calls return False (not ready), 3rd returns True
            mock_daemon.return_value.is_running.side_effect = [False, False, True]
            with patch('subprocess.Popen') as mock_popen:
                mock_proc = MagicMock()
                mock_proc.wait.return_value = 0
                mock_popen.return_value = mock_proc

                _start_daemon_service("/path/to/ledger.log", "test_service", {}, ["test", "args"])

                # Should have called is_running 3 times (polling)
                assert mock_daemon.return_value.is_running.call_count == 3
