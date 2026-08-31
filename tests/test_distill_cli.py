"""Tests Fase 6 — Comando distill (exponiendo _distill.py)."""

from causadb.cli._cmd_distill import cmd_distill
from causadb._distill import distill
import json
import os
import tempfile
from causadb._ledger_writer import LedgerWriter
from causadb._event_types import EventType
from causadb._event_schema import CanonicalEvent

def test_distill_cli_empty_ledger():
    """Test que maneja ledger vacío correctamente."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        
        # Solo crear ledger con génesis
        writer = LedgerWriter(ledger_path)
        genesis = CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        )
        writer.append(genesis)
        
        args = type('Args', (), {'ledger': ledger_path, 'format': 'json'})()
        exit_code, output = cmd_distill(args)
        assert exit_code == 0
        
        result = json.loads(output)
        assert result["skills"] == []

def test_distill_cli_with_data():
    """Test con datos suficientes para generar skills."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        writer = LedgerWriter(ledger_path)
        
        # Génesis
        writer.append(CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        ))
        
        # Simular algunos eventos FILE_MODIFIED y LLM_INVOKED para generar skills
        for i in range(3):
            writer.append(CanonicalEvent(
                event_type=EventType.FILE_MODIFIED,
                ctx_id="test_ctx",
                source="test:agent",
                source_type="agent",
                payload={"path": f"file_{i}.py", "action": "create"}
            ))
            
            writer.append(CanonicalEvent(
                event_type=EventType.LLM_INVOKED,
                ctx_id="test_ctx",
                source="test:agent",
                source_type="agent",
                payload={"prompt": f"This is test prompt number {i} for testing purposes."}
            ))
            # Add a tool call for every other iteration to get at least two tool events with the same tool
            if i % 2 == 0:  # i=0 and i=2
                writer.append(CanonicalEvent(
                    event_type=EventType.TOOL_CALLED,
                    ctx_id="test_ctx",
                    source="test:agent",
                    source_type="agent",
                    payload={"tool_name": "test_tool", "arguments": {}, "result": "success"}
                ))
        
        # Probar formato JSON
        args = type('Args', (), {'ledger': ledger_path, 'format': 'json'})()
        exit_code, output = cmd_distill(args)
        assert exit_code == 0
        
        result = json.loads(output)
        assert "skills" in result
        assert len(result["skills"]) >= 2, f"expected at least 2 skill types, got {len(result['skills'])}: {result['skills']}"
        
        # Verificar que tenemos file_tree y tool_patterns skills con contenido real
        skill_types = [s["type"] for s in result["skills"]]
        assert "file_tree" in skill_types
        assert "tool_patterns" in skill_types
        # Anti-teatro: verificar que file_tree tiene contenido (no es stub vacío)
        file_tree_skill = next(s for s in result["skills"] if s["type"] == "file_tree")
        assert file_tree_skill.get("content"), "file_tree skill should have non-empty content"
        
        # Probar formato terminal
        args = type('Args', (), {'ledger': ledger_path, 'format': 'terminal'})()
        exit_code, output = cmd_distill(args)
        assert exit_code == 0
        assert "Distill Result" in output
        assert "file_tree" in output

def test_distill_cli_invalid_format():
    """Test que maneja formato inválido."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = os.path.join(tmpdir, "ledger.log")
        # Solo génesis para evitar errores complejos
        writer = LedgerWriter(ledger_path)
        writer.append(CanonicalEvent(
            event_type=EventType.SYSTEM_BOOT,
            ctx_id="genesis",
            source="test:genesis",
            source_type="agent",
            payload={}
        ))
        
        args = type('Args', (), {'ledger': ledger_path, 'format': 'invalid'})()
        exit_code, output = cmd_distill(args)
        assert exit_code == 1
        assert "unknown format: invalid" in output