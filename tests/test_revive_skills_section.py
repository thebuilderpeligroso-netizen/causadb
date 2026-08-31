"""Fase 14.2 — Tests for revive skills section.

Artículo III (test-first), Artículo IX (fixture real, no mocks).

Cobertura:
  1. Revive shows skills table filtered by session_type (abrupt_close / normal_close).
  2. Revive shows file_tree skill content (normal_close).
  3. Revive with zero skills (first_run) doesn't crash.
"""

import argparse
import json
import os

import pytest

from causadb._init import causadb_init
from causadb._skill_registry import register_skill
from causadb.cli._cmd_revive import cmd_revive


def _make_revive_args(ledger, fmt="markdown"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--format", default="markdown")
    parser.add_argument("--decisions", type=int, default=10)
    parser.add_argument("--write", default=None)
    parser.add_argument("--last", action="store_true", default=False)
    return parser.parse_args(["--ledger", ledger, "--format", fmt])


def _sample_skill(
    skill_type="file_tree",
    name="test",
    confidence=0.8,
    tokens=100,
    content="x",
    source_session="s",
):
    return {
        "skill_type": skill_type,
        "skill_name": name,
        "content": content,
        "token_count": tokens,
        "confidence": confidence,
        "source_session": source_session,
    }


def _make_abrupt_close_ledger(tmp_path):
    """Create ledger + OCB dir with OCB_ACTIVE.log (abrupt_close)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    ocb_dir = os.path.join(os.path.dirname(ledger), "ocb")
    os.makedirs(ocb_dir, exist_ok=True)
    active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
    with open(active_path, "w") as f:
        f.write('{}\n')
    return ledger


def _make_normal_close_ledger(tmp_path):
    """Create ledger + OCB dir with OCB_SUMMARY.json + session file (normal_close)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    ocb_dir = os.path.join(os.path.dirname(ledger), "ocb")
    os.makedirs(ocb_dir, exist_ok=True)
    summary_path = os.path.join(ocb_dir, "OCB_SUMMARY.json")
    with open(summary_path, "w") as f:
        json.dump({"sedimentada": True, "work_done": "test"}, f)
    session_path = os.path.join(ocb_dir, "OCB_SESSION_1234567890.log")
    with open(session_path, "w") as f:
        f.write('{"event_id": "x", "event_type": "FILE_MODIFIED"}\n')
    return ledger


def test_revive_abrupt_close_shows_decisions_and_tool_patterns(tmp_path):
    """abrupt_close → shows decisions and tool_patterns skills only."""
    ledger = _make_abrupt_close_ledger(tmp_path)

    register_skill(ledger, _sample_skill(
        skill_type="decisions", name="decision_hashes",
        content="Decision: abc123\nDecision: def456",
        confidence=0.7, tokens=30,
    ))
    register_skill(ledger, _sample_skill(
        skill_type="tool_patterns", name="repeated_tools",
        content="tool 'read' used 3 times",
        confidence=0.3, tokens=20,
    ))
    # file_tree should NOT appear in abrupt_close
    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="touched_files_tree",
        content="causadb/\n- _daemon.py",
        confidence=1.0, tokens=50,
    ))

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0, f"revive failed: {output}"

    # Verify the skills table header
    assert "## Skills disponibles" in output

    # abrupt_close → decisions + tool_patterns only
    assert "decisions" in output
    assert "tool_patterns" in output
    assert "decision_hashes" in output
    assert "repeated_tools" in output

    # file_tree should NOT be in output
    assert "file_tree" not in output
    assert "touched_files_tree" not in output


def test_revive_normal_close_shows_file_tree_and_conventions(tmp_path):
    """normal_close → shows file_tree and conventions skills only."""
    ledger = _make_normal_close_ledger(tmp_path)

    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="touched_files_tree",
        content="causadb/\n- _daemon.py\n- _ledger_writer.py",
        confidence=1.0, tokens=50,
    ))
    register_skill(ledger, _sample_skill(
        skill_type="conventions", name="project_conventions",
        content="Use snake_case for variables",
        confidence=0.8, tokens=25,
    ))
    # decisions + tool_patterns should NOT appear in normal_close
    register_skill(ledger, _sample_skill(
        skill_type="decisions", name="decision_hashes",
        content="Decision: abc123",
        confidence=0.7, tokens=30,
    ))
    register_skill(ledger, _sample_skill(
        skill_type="tool_patterns", name="repeated_tools",
        content="tool 'read' used 3 times",
        confidence=0.3, tokens=20,
    ))

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0, f"revive failed: {output}"

    # Verify the skills table header
    assert "## Skills disponibles" in output

    # normal_close → file_tree + conventions only
    assert "file_tree" in output
    assert "conventions" in output
    assert "touched_files_tree" in output
    assert "project_conventions" in output

    # decisions + tool_patterns should NOT be in output
    assert "decisions" not in output
    assert "tool_patterns" not in output
    assert "decision_hashes" not in output
    assert "repeated_tools" not in output


def test_revive_shows_file_tree_content(tmp_path):
    """normal_close → shows file_tree skill content."""
    ledger = _make_normal_close_ledger(tmp_path)

    file_tree_content = "causadb/\n- _daemon.py\n- _harvester.py\n- _skill_registry.py\ntests/\n- test_skill_registry.py"

    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="touched_files_tree",
        content=file_tree_content,
        confidence=1.0, tokens=60,
    ))

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0

    # The file_tree content should appear in a code block
    assert "touched_files_tree" in output
    assert "_daemon.py" in output
    assert "_harvester.py" in output
    assert "_skill_registry.py" in output
    assert "test_skill_registry.py" in output

    # Verify it's in a code block
    assert "```" in output


def test_revive_with_zero_skills_shows_no_skills_section(tmp_path):
    """Verify revive output when no skills present doesn't crash and
    doesn't show the skills section."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0, f"revive failed: {output}"

    # No skills section should appear
    assert "## Skills disponibles" not in output, (
        f"expected no skills section when no skills, got:\n{output[:500]}"
    )

    # The old "Skills Relevantes" section should also not appear
    assert "## Skills Relevantes" not in output, (
        f"old 'Skills Relevantes' section should not appear, got:\n{output[:500]}"
    )


def test_revive_file_tree_truncated_when_huge(tmp_path):
    """normal_close → file_tree skill truncated when huge."""
    ledger = _make_normal_close_ledger(tmp_path)

    # 3000 lines, but tokens under max_tokens (2048) so content truncation happens at render level
    file_tree_content = "\n".join(f"causadb/file_{i}.py" for i in range(3000))

    register_skill(ledger, _sample_skill(
        skill_type="file_tree", name="touched_files_tree",
        content=file_tree_content,
        confidence=1.0, tokens=1500,  # under max_tokens (2048)
    ))

    args = _make_revive_args(ledger, "markdown")
    exit_code, output = cmd_revive(args)

    assert exit_code == 0
    # First line should be there
    assert "causadb/file_0.py" in output
    # Line 200 should be the last one (FILE_TREE_MAX_LINES = 200)
    assert "causadb/file_199.py" in output
    # Last line should NOT be there
    assert "causadb/file_2999.py" not in output
    # Check for the truncation marker
    assert "[...] +2800 líneas más — usá causadb_skill_list (skill_type=file_tree) para ver el mapa completo" in output