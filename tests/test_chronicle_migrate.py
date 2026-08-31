"""Tests for Phase 7 — Chronicle as a CausaDB resource.

Article III: Tests written BEFORE implementation.
Article IX: Anti-teatro — every test has discriminatory power.
"""

import json
import os
import pytest

from causadb._event_types import EventType
from causadb._event_schema import CanonicalEvent
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb.cli.main import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


@pytest.fixture
def chronicle_md(tmp_path):
    """Minimal chronicle fixture with 2 BIT entries."""
    content = """# CAUSADB_CHRONICLE.md

## BIT-7.1 — F.7.1 — EU AI Act Art. 12 compliance report
**Fecha:** 2026-07-23
**Maker:** gemini-cli
**Checker:** gemini-cli
**Archivos tocados:** causadb/_compliance.py, tests/test_eu_ai_act_compliance.py
**Resumen:** Implementado reporte de compliance EU AI Act Art. 12 con causal chain traversal.

## BIT-7.2 — F.7.2 — NIST AI RMF traceability compliance
**Fecha:** 2026-07-23
**Maker:** subagente coder
**Checker:** subagente audit
**Archivos tocados:** causadb/_compliance.py, tests/test_nist_ai_rmf_compliance.py
**Resumen:** Implementado reporte NIST AI RMF con traceability matrix.
"""
    path = tmp_path / "CAUSADB_CHRONICLE.md"
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# 7.4 — parse_chronicle_md() tests
# ---------------------------------------------------------------------------

def test_parse_chronicle_md_returns_list(chronicle_md):
    """parse_chronicle_md() must return a list of dicts."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert isinstance(entries, list)
    assert len(entries) == 2


def test_parse_chronicle_md_extracts_bit_id(chronicle_md):
    """Each entry must have a 'bit_id' field."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert entries[0]["bit_id"] == "BIT-7.1"
    assert entries[1]["bit_id"] == "BIT-7.2"


def test_parse_chronicle_md_extracts_title(chronicle_md):
    """Each entry must have a 'title' field extracted from after the —."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert "EU AI Act Art. 12" in entries[0]["title"]
    assert "NIST AI RMF" in entries[1]["title"]


def test_parse_chronicle_md_extracts_date(chronicle_md):
    """Each entry must have a 'date' field."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert entries[0]["date"] == "2026-07-23"
    assert entries[1]["date"] == "2026-07-23"


def test_parse_chronicle_md_extracts_maker(chronicle_md):
    """Each entry must have a 'maker' field."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert entries[0]["maker"] == "gemini-cli"
    assert entries[1]["maker"] == "subagente coder"


def test_parse_chronicle_md_extracts_checker(chronicle_md):
    """Each entry must have a 'checker' field."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert entries[0]["checker"] == "gemini-cli"
    assert entries[1]["checker"] == "subagente audit"


def test_parse_chronicle_md_extracts_files_touched(chronicle_md):
    """Each entry must have a 'files_touched' list."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert "causadb/_compliance.py" in entries[0]["files_touched"]
    assert "tests/test_eu_ai_act_compliance.py" in entries[0]["files_touched"]
    assert "tests/test_nist_ai_rmf_compliance.py" in entries[1]["files_touched"]


def test_parse_chronicle_md_extracts_summary(chronicle_md):
    """Each entry must have a 'summary' field."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md(chronicle_md)
    assert "EU AI Act" in entries[0]["summary"]
    assert "NIST AI RMF" in entries[1]["summary"]


def test_parse_chronicle_md_empty_file(tmp_path):
    """Empty chronicle returns empty list."""
    from causadb._chronicle_migrate import parse_chronicle_md
    path = tmp_path / "empty.md"
    path.write_text("# Empty\n")
    entries = parse_chronicle_md(str(path))
    assert entries == []


def test_parse_chronicle_md_nonexistent_file():
    """Non-existent file returns empty list gracefully."""
    from causadb._chronicle_migrate import parse_chronicle_md
    entries = parse_chronicle_md("/nonexistent/path/chronicle.md")
    assert entries == []


# ---------------------------------------------------------------------------
# 7.2 — ReplayEngine apply() with CHRONICLE_ENTRY
# ---------------------------------------------------------------------------

def test_replay_apply_chronicle_entry_appends_to_state(ledger_path):
    """apply() with CHRONICLE_ENTRY must append to state['chronicle_entries']."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(
        event_type=EventType.CHRONICLE_ENTRY,
        ctx_id="ctx",
        source="causadb:chronicle",
        payload={
            "bit_id": "BIT-001",
            "title": "Test Entry",
            "date": "2026-08-02",
            "maker": "tester",
            "checker": "tester",
            "summary": "Test summary",
            "files_touched": ["file1.py", "file2.py"],
        },
    )
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert "chronicle_entries" in state
    assert len(state["chronicle_entries"]) == 1
    entry = state["chronicle_entries"][0]
    assert entry["bit_id"] == "BIT-001"
    assert entry["title"] == "Test Entry"
    assert entry["date"] == "2026-08-02"
    assert entry["maker"] == "tester"
    assert entry["checker"] == "tester"
    assert entry["summary"] == "Test summary"
    assert entry["files_touched"] == ["file1.py", "file2.py"]


def test_replay_apply_multiple_chronicle_entries(ledger_path):
    """Multiple CHRONICLE_ENTRY events accumulate in state."""
    writer = LedgerWriter(ledger_path)
    for i in range(3):
        e = CanonicalEvent(
            event_type=EventType.CHRONICLE_ENTRY,
            ctx_id="ctx",
            source="ctx:chronicle",
            payload={
                "bit_id": f"BIT-{i:03d}",
                "title": f"Entry {i}",
                "date": "2026-08-02",
                "maker": "tester",
                "checker": "tester",
                "summary": f"Summary {i}",
                "files_touched": [f"file{i}.py"],
            },
        )
        writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["chronicle_entries"]) == 3
    assert state["chronicle_entries"][0]["bit_id"] == "BIT-000"
    assert state["chronicle_entries"][2]["bit_id"] == "BIT-002"


def test_replay_initial_state_has_chronicle_entries(ledger_path):
    """Initial state must include chronicle_entries key (empty list)."""
    open(ledger_path, "a").close()
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert "chronicle_entries" in state
    assert state["chronicle_entries"] == []


# ---------------------------------------------------------------------------
# 7.3a — CLI `causadb chronicle append`
# ---------------------------------------------------------------------------

def test_cli_chronicle_append_creates_event(ledger_path, capsys):
    """`causadb chronicle append` must write a CHRONICLE_ENTRY to the ledger."""
    rc, out = _run_cli([
        "chronicle", "append",
        "--ledger", ledger_path,
        "--bit", "BIT-001",
        "--title", "Test Entry",
        "--date", "2026-08-02",
        "--maker", "tester",
        "--checker", "tester",
        "--summary", "A test summary.",
        "--files", "file1.py", "file2.py",
    ], capsys)

    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "event_id" in payload
    assert "hash" in payload

    # Verify the event is in the ledger
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["chronicle_entries"]) == 1
    entry = state["chronicle_entries"][0]
    assert entry["bit_id"] == "BIT-001"
    assert entry["title"] == "Test Entry"
    assert entry["maker"] == "tester"
    assert entry["files_touched"] == ["file1.py", "file2.py"]


def test_cli_chronicle_append_missing_required_fields(ledger_path, capsys):
    """`causadb chronicle append` without required fields must fail."""
    rc, out = _run_cli([
        "chronicle", "append",
        "--ledger", ledger_path,
        "--bit", "BIT-001",
    ], capsys)

    assert rc == 1, f"expected exit 1 for missing fields, got {rc}; stdout={out!r}"


def test_cli_chronicle_append_appears_in_replay_chronicle(ledger_path, capsys):
    """CHRONICLE_ENTRY written via CLI must appear in `replay --chronicle`."""
    # First, append a chronicle entry
    rc1, out1 = _run_cli([
        "chronicle", "append",
        "--ledger", ledger_path,
        "--bit", "BIT-001",
        "--title", "Test Entry",
        "--date", "2026-08-02",
        "--maker", "tester",
        "--checker", "tester",
        "--summary", "Test summary.",
        "--files", "file1.py",
    ], capsys)
    assert rc1 == 0, f"append failed: {out1}"

    # Now replay with --chronicle flag
    rc2, out2 = _run_cli([
        "replay",
        "--ledger", ledger_path,
        "--chronicle", "1",
    ], capsys)
    assert rc2 == 0, f"replay failed: {out2}"
    state = json.loads(out2)
    assert "chronicle_entries" in state
    assert len(state["chronicle_entries"]) == 1
    assert state["chronicle_entries"][0]["bit_id"] == "BIT-001"


# ---------------------------------------------------------------------------
# 7.4 — CLI `causadb chronicle migrate`
# ---------------------------------------------------------------------------

def test_cli_chronicle_migrate_writes_entries(ledger_path, chronicle_md, capsys):
    """`causadb chronicle migrate` must parse chronicle and write entries to ledger."""
    rc, out = _run_cli([
        "chronicle", "migrate",
        "--ledger", ledger_path,
        "--chronicle-path", chronicle_md,
    ], capsys)

    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert payload.get("status") == "ok"
    assert payload.get("entries_migrated") == 2

    # Verify entries are in the ledger
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["chronicle_entries"]) == 2
    assert state["chronicle_entries"][0]["bit_id"] == "BIT-7.1"
    assert state["chronicle_entries"][1]["bit_id"] == "BIT-7.2"


def test_cli_chronicle_migrate_missing_chronicle(ledger_path, capsys):
    """`causadb chronicle migrate` with non-existent chronicle must fail gracefully."""
    rc, out = _run_cli([
        "chronicle", "migrate",
        "--ledger", ledger_path,
        "--chronicle-path", "/nonexistent/chronicle.md",
    ], capsys)

    assert rc == 1, f"expected exit 1 for missing chronicle, got {rc}; stdout={out!r}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli(args, capsys):
    """Run the CLI with the given args list, return (exit_code, stdout_str)."""
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out