"""
Tests for the Obsidian Plugin Adapter (G.2).

Verifies that ``log_note_change`` and ``query_notes_by_path`` correctly
delegate to ``causadb.adapters.template`` and produce valid ledger entries.
"""

import json

from causadb.adapters.obsidian.adapter import log_note_change, query_notes_by_path


def test_obsidian_logs_note_change(tmp_path):
    """Escribe un FILE_MODIFIED en el ledger y verifica su payload."""
    ledger = str(tmp_path / "ledger.log")

    result = log_note_change("vault/ideas.md", "Ideas", ledger_path=ledger)

    # --- Assert return value ---
    assert "event_id" in result, "Result must contain event_id"
    assert "hash" in result, "Result must contain hash"
    assert "timestamp" in result, "Result must contain timestamp"

    # --- Assert ledger file content ---
    with open(ledger, "r") as f:
        lines = f.readlines()
    assert len(lines) == 1, "Ledger debe tener exactamente 1 entrada"

    entry = json.loads(lines[0])
    event = entry["event"]

    assert event["event_type"] == "FILE_MODIFIED"
    assert event["payload"]["path"] == "vault/ideas.md"
    assert event["payload"]["title"] == "Ideas"
    assert event["payload"]["action"] == "edit"
    assert event["source"] == "obsidian"


def test_obsidian_queries_by_note(tmp_path):
    """Escribe 2 notas y verifica que query por path retorne solo la que coincide."""
    ledger = str(tmp_path / "ledger.log")

    log_note_change("vault/ideas.md", "Ideas", ledger_path=ledger)
    log_note_change("vault/tasks.md", "Tareas", ledger_path=ledger)

    # --- Query by first note path ---
    events = query_notes_by_path("vault/ideas.md", ledger_path=ledger)
    assert len(events) == 1, (
        f"Esperaba 1 evento para 'vault/ideas.md', obtuve {len(events)}"
    )
    assert events[0]["event"]["payload"]["path"] == "vault/ideas.md"
    assert events[0]["event"]["payload"]["title"] == "Ideas"

    # --- Query by second note path ---
    events = query_notes_by_path("vault/tasks.md", ledger_path=ledger)
    assert len(events) == 1, (
        f"Esperaba 1 evento para 'vault/tasks.md', obtuve {len(events)}"
    )
    assert events[0]["event"]["payload"]["path"] == "vault/tasks.md"
    assert events[0]["event"]["payload"]["title"] == "Tareas"

    # --- Query by non-existent path returns empty ---
    events = query_notes_by_path("vault/nonexistent.md", ledger_path=ledger)
    assert len(events) == 0, (
        f"Esperaba 0 eventos para path inexistente, obtuve {len(events)}"
    )
