import pytest
from types import MappingProxyType
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_revive import cmd_revive
import argparse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_revive_args(ledger, fmt="markdown"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    return parser.parse_args(["--ledger", ledger, "--format", fmt])

def _make_ledger_with_decisions(tmp_path, decisions_spec):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    
    # Decisions y status changed
    for i, (impact, decision_type, origin, reasoning, status) in enumerate(decisions_spec):
        gd_event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "reasoning": reasoning,
                "impact": impact,
                "decision_type": decision_type,
                "origin": origin,
            }),
        )
        written = writer.append(gd_event)
        gd_event_id = written["event"]["event_id"]
        
        if status:
            status_event = CanonicalEvent(
                event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
                ctx_id="test",
                source="causadb:test",
                parent_event_id=gd_event_id,
                payload=MappingProxyType({
                    "new_status": status,
                }),
            )
            writer.append(status_event)
    return ledger

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_revive_separates_active_vs_historical_decisions(tmp_path):
    # 2 activas (proposed), 2 historicas (done)
    ledger = _make_ledger_with_decisions(tmp_path, [
        ("high", "strategic", "agent", "Active decision 1", "proposed"),
        ("low", "tactical", "distill", "Active decision 2", "in_progress"),
        ("medium", "strategic", "agent", "Historical decision 1", "done"),
        ("high", "architectural", "agent", "Historical decision 2", "superseded"),
    ])
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    
    assert "**Decisiones activas:**" in output
    assert "**Decisiones historicas:**" in output
    
    # Verificar orden de secciones y contenido
    active_section = output.split("**Decisiones activas:**")[1].split("**Decisiones historicas:**")[0]
    historical_section = output.split("**Decisiones historicas:**")[1]
    
    assert "Active decision 1" in active_section
    assert "Active decision 2" in active_section
    assert "Historical decision 1" in historical_section
    assert "Historical decision 2" in historical_section

def test_revive_no_active_section_when_all_done(tmp_path):
    ledger = _make_ledger_with_decisions(tmp_path, [
        ("medium", "strategic", "agent", "Historical decision 1", "done"),
        ("high", "architectural", "agent", "Historical decision 2", "superseded"),
    ])
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    
    assert "**Decisiones historicas:**" in output
    # Anti-teatro: asegurar que no aparece
    assert "**Decisiones activas:**" not in output

# --- R.3.3: PROJECT_SNAPSHOT en Revive ---

def _make_ledger_with_snapshots(tmp_path, snapshots_spec):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    
    for spec in snapshots_spec:
        event = CanonicalEvent(
            event_type=EventType.PROJECT_SNAPSHOT,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType(spec),
        )
        writer.append(event)
    return ledger

def test_revive_shows_last_project_snapshot_only(tmp_path):
    ledger = _make_ledger_with_snapshots(tmp_path, [
        {"total_events": 1, "total_tests": 1, "fases_completadas": ["R.1"], "bloqueantes_resueltos": 0, "notas": "1"},
        {"total_events": 2, "total_tests": 2, "fases_completadas": ["R.1", "R.2"], "bloqueantes_resueltos": 0, "notas": "2"},
        {"total_events": 3, "total_tests": 3, "fases_completadas": ["R.1", "R.2", "R.3"], "bloqueantes_resueltos": 0, "notas": "3"},
    ])
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    
    assert "3" in output
    assert "Notas: \"1\"" not in output
    assert "Notas: \"2\"" not in output

def test_revive_hides_snapshot_section_when_empty(tmp_path):
    # Ledger sin snapshots
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    
    assert "## Project Snapshot" not in output

def test_revive_snapshot_appears_before_technical_state(tmp_path):
    ledger = _make_ledger_with_snapshots(tmp_path, [
        {"total_events": 1, "total_tests": 1, "fases_completadas": ["R.1"], "bloqueantes_resueltos": 0, "notas": "1"},
    ])
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    
    snapshot_idx = output.find("## Project Snapshot")
    tech_state_idx = output.find("## Estado Técnico")
    
    assert snapshot_idx != -1
    assert tech_state_idx != -1
    assert snapshot_idx < tech_state_idx

# ---------------------------------------------------------------------------
# Revive Richer: Score + Daemon + Sync
# ---------------------------------------------------------------------------

def test_revive_includes_score_section(tmp_path):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    
    # Log some events so score has data
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="test", source="causadb:test",
        payload={"path": "test.py", "action": "create"},
    )
    writer.append(event)
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Score de Productividad" in output
    assert "/100" in output

def test_revive_includes_daemon_section(tmp_path):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Estado del Daemon" in output
    assert "vigilante" in output
    assert "mcp_proxy" in output
    assert "proxy_server" in output

def test_revive_ignores_sync_when_not_configured(tmp_path):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Estado de Sincronización" not in output

def test_revive_score_graceful_fallback(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    # Mock compute_score to fail
    import causadb._score as score_mod
    monkeypatch.setattr(score_mod, "compute_score", lambda *a, **kw: (_ for _ in ()).throw(Exception("score fail")))
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Score de Productividad" not in output

def test_revive_daemon_graceful_fallback(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    import causadb._daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "is_running", lambda *a, **kw: (_ for _ in ()).throw(Exception("daemon fail")))
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Estado del Daemon" not in output

def test_revive_sync_section_when_configured(tmp_path):
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    # Create sync state manually
    import json, os
    sync_dir = os.path.dirname(ledger)
    sync_state = {
        "hub_url": "https://hub.causadb.test",
        "api_key": "test123",
        "last_synced_seq": 42,
        "interval_minutes": 30,
    }
    with open(os.path.join(sync_dir, "sync_state.json"), "w") as f:
        json.dump(sync_state, f)
    
    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)
    assert exit_code == 0
    assert "## Estado de Sincronización" in output
    assert "hub.causadb.test" in output
    assert "evento #42" in output
    assert "30 minutos" in output
