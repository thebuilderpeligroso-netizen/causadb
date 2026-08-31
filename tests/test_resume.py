"""R.1 — Tests for `causadb resume` command.

Tests verify:
- first_run detection (no OCB, no ledger events)
- abrupt_close detection (OCB_ACTIVE.log exists, no OCB_SUMMARY.json)
- normal_close detection (OCB_SUMMARY.json exists)
- ledger state reconstruction is merged with OCB context
- resume hints are actionable and correct
- --write produces RESUME.md on disk
- --format markdown produces markdown output
- anti-teatro: hints are not empty, they reference real state
"""

import json
import os
import tempfile

import pytest

from causadb.cli._cmd_resume import (
    generate_resume,
    generate_resume_markdown,
    _build_hints,
    _safe_replay,
    _load_relevant_skills,
)
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._skill_registry import register_skill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path):
    """Create a .causadb workspace with a ledger file."""
    causadb_dir = os.path.join(tmp_path, ".causadb")
    os.makedirs(causadb_dir)
    ledger = os.path.join(causadb_dir, "ledger.log")
    open(ledger, "w").close()
    return ledger


def _append_event(ledger_path, event_type, payload, source="causadb:test"):
    """Append a CanonicalEvent to the ledger via LedgerWriter."""
    writer = LedgerWriter(ledger_path)
    event = CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source=source,
        source_type="agent",
        payload=payload,
    )
    writer.append(event)


def _make_ocb_dir(ledger_path):
    """Create the OCB directory next to the ledger."""
    ocb_dir = os.path.join(os.path.dirname(ledger_path), "ocb")
    os.makedirs(ocb_dir, exist_ok=True)
    return ocb_dir


# ---------------------------------------------------------------------------
# first_run
# ---------------------------------------------------------------------------

class TestFirstRun:
    def test_first_run_no_ocb_no_events(self, tmp_path):
        """Empty workspace → first_run, zero state."""
        ledger = _make_workspace(tmp_path)
        resume = generate_resume(ledger)

        assert resume["session_type"] == "first_run"
        assert resume["events_count"] == 0
        assert resume["files_modified"] == 0
        assert resume["llm_invocations"] == 0
        assert resume["resume_hints"] == [
            "This is a fresh workspace — no previous session to resume."
        ]

    def test_first_run_markdown_render(self, tmp_path):
        """first_run should render valid markdown."""
        ledger = _make_workspace(tmp_path)
        resume = generate_resume(ledger)
        md = generate_resume_markdown(resume)

        assert "# CausaDB Session Resume" in md
        assert "first_run" in md
        assert "Events applied" in md


# ---------------------------------------------------------------------------
# abrupt_close
# ---------------------------------------------------------------------------

class TestAbruptClose:
    def test_abrupt_close_detected(self, tmp_path):
        """OCB_ACTIVE.log exists but no OCB_SUMMARY.json → abrupt_close."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        # Create OCB_ACTIVE.log (simulates unclean shutdown)
        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{"event_type": "FILE_MODIFIED", "ctx_id": "test"}\n')

        # Add a file modification event to the ledger
        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "modified", "path": "/tmp/main.py"}),
        )

        resume = generate_resume(ledger)

        assert resume["session_type"] == "abrupt_close"
        assert resume["files_modified"] == 1
        assert resume["last_file"]["path"] == "/tmp/main.py"

    def test_abrupt_close_hints_mention_abnormal(self, tmp_path):
        """Hints should mention the abnormal shutdown for abrupt_close."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{"event_type": "FILE_MODIFIED"}\n')

        resume = generate_resume(ledger)
        hints_text = " ".join(resume["resume_hints"])
        assert "abnormally" in hints_text.lower() or "abrupt" in hints_text.lower()

    def test_abrupt_close_with_llm_error(self, tmp_path):
        """If last LLM call errored, hints should warn about it."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.LLM_INVOKED,
            MappingProxyType({
                "model": "gpt-4",
                "prompt": "hello",
                "response_tokens": 0,
                "duration_ms": 100,
                "error": "Connection timeout",
            }),
        )

        resume = generate_resume(ledger)
        hints_text = " ".join(resume["resume_hints"])
        assert "Connection timeout" in hints_text
        assert "WARNING" in hints_text


# ---------------------------------------------------------------------------
# normal_close
# ---------------------------------------------------------------------------

class TestNormalClose:
    def test_normal_close_detected(self, tmp_path):
        """OCB_SUMMARY.json + OCB_SESSION_*.log → normal_close.

        F2 (M1) gap residual 4: un summary huérfano (sin session file ni
        active ni partitions) es abrupt_close. El session file es la
        evidencia de cierre limpio."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        # Create OCB_SUMMARY.json (clean shutdown)
        summary_path = os.path.join(ocb_dir, "OCB_SUMMARY.json")
        with open(summary_path, "w") as f:
            json.dump({"sedimentada": True, "work_done": "Tests for proxy"}, f)
        # Evidencia de cierre limpio: session file
        session_path = os.path.join(ocb_dir, "OCB_SESSION_1234567890.log")
        with open(session_path, "w") as f:
            f.write('{"event_id": "x", "event_type": "FILE_MODIFIED"}\n')

        resume = generate_resume(ledger)

        assert resume["session_type"] == "normal_close"
        assert resume["ocb_summary"]["sedimentada"] is True
        assert resume["ocb_summary"]["work_done"] == "Tests for proxy"


# ---------------------------------------------------------------------------
# Ledger state reconstruction
# ---------------------------------------------------------------------------

class TestLedgerStateReconstruction:
    def test_resume_includes_reasoning_steps(self, tmp_path):
        """Resume should count reasoning steps from the ledger."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.REASONING_STEP,
            MappingProxyType({
                "model": "gpt-4",
                "reasoning": "Step 1: analyze. Step 2: conclude.",
            }),
        )

        resume = generate_resume(ledger)
        assert resume["reasoning_steps"] == 1

    def test_resume_includes_cost(self, tmp_path):
        """Resume should sum cost from COST_ACCOUNTED events."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.COST_ACCOUNTED,
            MappingProxyType({
                "model": "gpt-4",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost": 0.0015,
                "currency": "USD",
            }),
        )

        resume = generate_resume(ledger)
        assert resume["total_cost_usd"] == pytest.approx(0.0015)


# ---------------------------------------------------------------------------
# Resume hints
# ---------------------------------------------------------------------------

class TestHints:
    def test_hints_not_empty_on_abrupt_close(self, tmp_path):
        """Hints should not be empty for abrupt_close."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "modified", "path": "/tmp/test.py"}),
        )

        resume = generate_resume(ledger)
        assert len(resume["resume_hints"]) > 0

    def test_hints_mention_last_file(self, tmp_path):
        """Hints should mention the last file modified."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "modified", "path": "/src/main.py"}),
        )

        resume = generate_resume(ledger)
        hints_text = " ".join(resume["resume_hints"])
        assert "/src/main.py" in hints_text

    def test_hints_mention_tool_errors(self, tmp_path):
        """Hints should warn about tool call errors."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.TOOL_CALLED,
            MappingProxyType({
                "tool_name": "bash",
                "arguments": "rm -rf /",
                "result": None,
                "duration_ms": 50,
                "error": "Permission denied",
            }),
        )

        resume = generate_resume(ledger)
        hints_text = " ".join(resume["resume_hints"])
        assert "error" in hints_text.lower()


# ---------------------------------------------------------------------------
# _safe_replay degradación suave
# ---------------------------------------------------------------------------

class TestSafeReplay:
    def test_safe_replay_empty_ledger(self, tmp_path):
        """Empty ledger → initial state, no crash."""
        ledger = _make_workspace(tmp_path)
        state = _safe_replay(ledger)
        assert isinstance(state, dict)
        assert state.get("events_applied", 0) == 0

    def test_safe_replay_corrupt_ledger(self, tmp_path):
        """Corrupt ledger → empty state, no crash (degradación suave)."""
        ledger = _make_workspace(tmp_path)
        with open(ledger, "w") as f:
            f.write("not valid json\n")
            f.write("also not valid\n")

        state = _safe_replay(ledger)
        assert state == {}


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    def test_resume_cli_first_run(self, tmp_path, capsys):
        """`causadb resume` on a fresh workspace → first_run in JSON."""
        from causadb.cli.main import main

        project = os.path.join(tmp_path, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()

        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["resume"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out)
            assert result["session_type"] == "first_run"
        finally:
            os.chdir(cwd)

    def test_resume_cli_markdown_format(self, tmp_path, capsys):
        """`causadb resume --format markdown` → markdown text."""
        from causadb.cli.main import main

        project = os.path.join(tmp_path, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()

        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["resume", "--format", "markdown"])
            assert rc == 0
            out = capsys.readouterr().out
            assert "# CausaDB Session Resume" in out
        finally:
            os.chdir(cwd)

    def test_resume_cli_write_flag(self, tmp_path, capsys):
        """`causadb resume --write` → RESUME.md on disk."""
        from causadb.cli.main import main

        project = os.path.join(tmp_path, "proj")
        os.makedirs(project)
        rc_init = main(["init", project])
        assert rc_init == 0
        capsys.readouterr()

        cwd = os.getcwd()
        os.chdir(project)
        try:
            rc = main(["resume", "--write"])
            assert rc == 0
            out = capsys.readouterr().out
            result = json.loads(out)
            resume_path = result.get("resume_md_path", "")
            assert resume_path != ""
            assert os.path.exists(resume_path)
            md_content = open(resume_path).read()
            assert "# CausaDB Session Resume" in md_content
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# Anti-teatro: markdown doesn't fake structure
# ---------------------------------------------------------------------------

class TestAntiTeatro:
    def test_markdown_contains_real_data_not_template(self, tmp_path):
        """Markdown should contain actual counts from the ledger, not placeholders."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "created", "path": "/src/hello.py"}),
        )
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "modified", "path": "/src/world.py"}),
        )

        resume = generate_resume(ledger)
        md = generate_resume_markdown(resume)

        assert "Files modified:** 2" in md
        assert "/src/hello.py" in md
        assert "/src/world.py" in md

    def test_hints_reference_actual_file_not_generic(self, tmp_path):
        """Hints should name the actual file, not a generic message."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)

        active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.FILE_MODIFIED,
            MappingProxyType({"action": "modified", "path": "/very/specific/path.py"}),
        )

        resume = generate_resume(ledger)
        hints_text = " ".join(resume["resume_hints"])
        assert "/very/specific/path.py" in hints_text


# ---------------------------------------------------------------------------
# F.13.4.5 — Resume Enhancement (Skills/Distill)
# ---------------------------------------------------------------------------

def _make_abrupt_close_workspace(tmp_path):
    """Create a workspace + OCB dir with OCB_ACTIVE.log (abrupt_close)."""
    ledger = _make_workspace(tmp_path)
    ocb_dir = _make_ocb_dir(ledger)
    active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
    with open(active_path, "w") as f:
        f.write('{}\n')
    return ledger


def _make_normal_close_workspace(tmp_path, summary=None):
    """Create a workspace + OCB dir with OCB_SUMMARY.json + OCB_SESSION_*.log
    (normal_close). F2 (M1) gap residual 4: un summary huérfano (sin
    session file ni active ni partitions) es abrupt_close, no normal_close.
    El session file es la evidencia de cierre limpio que close_session()
    genera al renombrar OCB_ACTIVE.log."""
    ledger = _make_workspace(tmp_path)
    ocb_dir = _make_ocb_dir(ledger)
    summary_path = os.path.join(ocb_dir, "OCB_SUMMARY.json")
    with open(summary_path, "w") as f:
        json.dump(summary or {"sedimentada": True, "work_done": "x"}, f)
    # Evidencia de cierre limpio: session file (close_session lo genera)
    session_path = os.path.join(ocb_dir, "OCB_SESSION_1234567890.log")
    with open(session_path, "w") as f:
        f.write('{"event_id": "x", "event_type": "FILE_MODIFIED"}\n')
    return ledger


def _register_skill(ledger_path, skill_type, name, tokens=100, confidence=0.8, content="sample content"):
    """Register a skill via the skill registry (ledger-first)."""
    skill_dict = {
        "skill_type": skill_type,
        "skill_name": name,
        "content": content,
        "token_count": tokens,
        "confidence": confidence,
        "source_session": "test-session",
    }
    return register_skill(ledger_path, skill_dict)


class TestResumeRelevantSkills:
    """F.13.4.5 — relevant_skills injection into resume output."""

    def test_resume_includes_relevant_skills_when_available(self, tmp_path):
        """#1: register skills → generate_resume → output contains relevant_skills."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        _register_skill(ledger, "decisions", "dec1", tokens=100)
        _register_skill(ledger, "tool_patterns", "tp1", tokens=100)

        resume = generate_resume(ledger)

        assert "relevant_skills" in resume, "relevant_skills key missing from resume"
        assert len(resume["relevant_skills"]) == 2, (
            f"expected 2 relevant skills, got {len(resume['relevant_skills'])}"
        )
        types = {s["skill_type"] for s in resume["relevant_skills"]}
        assert types == {"decisions", "tool_patterns"}

    def test_resume_first_run_no_skills(self, tmp_path):
        """#2: first_run → relevant_skills is empty (nothing to resume)."""
        ledger = _make_workspace(tmp_path)
        # Even if skills exist in the ledger, first_run should not load them.
        _register_skill(ledger, "file_tree", "tree1", tokens=100)

        resume = generate_resume(ledger)

        assert resume["session_type"] == "first_run"
        assert resume["relevant_skills"] == [], (
            f"first_run should have no relevant_skills, got {resume['relevant_skills']}"
        )

    def test_resume_abrupt_close_loads_decisions_and_tool_patterns(self, tmp_path):
        """#3: abrupt_close → only decisions and tool_patterns loaded."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        # Register skills of ALL types.
        _register_skill(ledger, "decisions", "d1", tokens=50)
        _register_skill(ledger, "tool_patterns", "tp1", tokens=50)
        _register_skill(ledger, "file_tree", "ft1", tokens=50)
        _register_skill(ledger, "conventions", "c1", tokens=50)

        resume = generate_resume(ledger)

        assert resume["session_type"] == "abrupt_close"
        types = {s["skill_type"] for s in resume["relevant_skills"]}
        assert types == {"decisions", "tool_patterns"}, (
            f"abrupt_close should load only decisions+tool_patterns, got {types}"
        )
        assert "file_tree" not in types
        assert "conventions" not in types

    def test_resume_normal_close_loads_file_tree_and_conventions(self, tmp_path):
        """#4: normal_close → only file_tree and conventions loaded."""
        ledger = _make_normal_close_workspace(tmp_path)
        _register_skill(ledger, "decisions", "d1", tokens=50)
        _register_skill(ledger, "tool_patterns", "tp1", tokens=50)
        _register_skill(ledger, "file_tree", "ft1", tokens=50)
        _register_skill(ledger, "conventions", "c1", tokens=50)

        resume = generate_resume(ledger)

        assert resume["session_type"] == "normal_close"
        types = {s["skill_type"] for s in resume["relevant_skills"]}
        assert types == {"file_tree", "conventions"}, (
            f"normal_close should load only file_tree+conventions, got {types}"
        )
        assert "decisions" not in types
        assert "tool_patterns" not in types

    def test_resume_skills_truncated_when_exceed_max_tokens(self, tmp_path):
        """#5: skill with token_count=3000, max_tokens=2048 → content truncated with [...]."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        # One skill that exceeds the default 2048 budget.
        big_content = "A" * 12000  # ~3000 tokens at 4 chars/token
        _register_skill(
            ledger, "decisions", "big_dec", tokens=3000, content=big_content
        )

        # Call the helper directly with the default budget.
        skills = _load_relevant_skills(ledger, "abrupt_close", max_tokens=2048)

        assert len(skills) == 1, f"expected 1 truncated skill, got {len(skills)}"
        skill = skills[0]
        assert skill["token_count"] <= 2048, (
            f"truncated skill token_count should be <= 2048, got {skill['token_count']}"
        )
        assert "[...]" in skill["content"], (
            "truncated content should end with [...]"
        )
        # The truncated content should be shorter than the original.
        assert len(skill["content"]) < len(big_content)

    def test_resume_skills_total_under_max_tokens_all_pass(self, tmp_path):
        """#6: skills summing to 1000 < 2048 → all included, no truncation."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        _register_skill(ledger, "decisions", "d1", tokens=400, content="dec content")
        _register_skill(ledger, "tool_patterns", "tp1", tokens=600, content="tp content")

        skills = _load_relevant_skills(ledger, "abrupt_close", max_tokens=2048)

        assert len(skills) == 2, f"expected 2 skills, got {len(skills)}"
        # No truncation marker should appear.
        for s in skills:
            assert "[...]" not in s["content"], (
                f"skill {s['skill_name']} should not be truncated"
            )
        # Token counts preserved.
        total = sum(s["token_count"] for s in skills)
        assert total == 1000

    def test_resume_markdown_includes_skills_section(self, tmp_path):
        """#7: generate_resume_markdown with relevant_skills → output contains '## Relevant Skills'."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        _register_skill(ledger, "decisions", "dec1", tokens=100, content="use pattern X")

        resume = generate_resume(ledger)
        md = generate_resume_markdown(resume)

        assert "## Relevant Skills" in md, "markdown should include Relevant Skills section"
        assert "dec1" in md, "markdown should include skill name"
        assert "decisions" in md, "markdown should include skill type"
        assert "use pattern X" in md, "markdown should include skill content"

    def test_resume_markdown_no_skills_section_when_empty(self, tmp_path):
        """#8: no skills → markdown does NOT contain '## Relevant Skills'."""
        ledger = _make_abrupt_close_workspace(tmp_path)
        # No skills registered.

        resume = generate_resume(ledger)
        md = generate_resume_markdown(resume)

        assert resume["relevant_skills"] == []
        assert "## Relevant Skills" not in md, (
            "markdown should NOT include Relevant Skills section when empty"
        )


# ---------------------------------------------------------------------------
# F.13.4.5 — Anti-teatro (Artículo IX)
# ---------------------------------------------------------------------------

class TestAntiTeatroResumeSkills:
    """Anti-teatro: mutating _load_relevant_skills to skip loading must
    break the tests above (test #9)."""

    def test_anti_teatro_resume_ignores_skills(self, tmp_path, monkeypatch):
        """#9: if _load_relevant_skills is mutated to return [] without
        calling load_skills, test #1 (relevant_skills present) must fail.

        This test verifies the anti-teatro property by simulating the
        mutation and asserting the resume does NOT contain relevant_skills
        — proving that test #1 would fail under such a mutation.
        """
        ledger = _make_abrupt_close_workspace(tmp_path)
        _register_skill(ledger, "decisions", "dec1", tokens=100)
        _register_skill(ledger, "tool_patterns", "tp1", tokens=100)

        # Mutate _load_relevant_skills to return [] (the "teatro" version
        # that skips the ledger replay).
        import causadb.cli._cmd_resume as resume_mod

        def _teatro_load(ledger_path, session_type, max_tokens=2048):
            return []  # noqa: teatro — does NOT call load_skills

        monkeypatch.setattr(resume_mod, "_load_relevant_skills", _teatro_load)

        resume = generate_resume(ledger)

        # Under the mutation, relevant_skills is empty — this proves
        # that test #1 (which asserts non-empty) would FAIL.
        assert resume["relevant_skills"] == [], (
            "Under the teatro mutation, relevant_skills should be empty. "
            "If this assertion fails, the mutation did not take effect."
        )
        # Sanity: confirm the real (un-mutated) function would have loaded them.
        monkeypatch.undo()
        resume_real = generate_resume(ledger)
        assert len(resume_real["relevant_skills"]) == 2, (
            "Un-mutated _load_relevant_skills should load 2 skills. "
            "If this fails, the test setup is wrong."
        )


# ---------------------------------------------------------------------------
# Fase 0 — Resumen de entrada = último SESSION_SUMMARY del replay
# ---------------------------------------------------------------------------

class TestEntrySummary:
    def test_resume_entry_summary(self, tmp_path):
        """Fase 0 (ajuste 2) — generate_resume expone ``entry_summary``
        con el último SESSION_SUMMARY del replay. El resumen de entrada
        SIEMPRE sale del ledger (Art. I), nunca del OCB."""
        ledger = _make_workspace(tmp_path)
        _make_ocb_dir(ledger)

        # OCB_ACTIVE.log (abrupt_close) para no ser first_run
        active_path = os.path.join(os.path.dirname(ledger), "ocb", "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        # Dos SESSION_SUMMARY en el ledger → entry_summary = el último
        from types import MappingProxyType
        _append_event(
            ledger,
            EventType.SESSION_SUMMARY,
            MappingProxyType({
                "tool": "opencode",
                "session_id": "sess-1",
                "turn_count": 5,
                "summary_lines": ["Primera sesión"],
                "decisions": [],
                "errors": [],
                "files_touched": [],
                "tokens_used": 1000,
                "duration_s": 30,
            }),
        )
        _append_event(
            ledger,
            EventType.SESSION_SUMMARY,
            MappingProxyType({
                "tool": "opencode",
                "session_id": "sess-2",
                "turn_count": 12,
                "summary_lines": ["Trabajamos en OCB ↔ BlobStore"],
                "decisions": ["externalizar payloads a $blob"],
                "errors": [],
                "files_touched": ["_ocb_manager.py"],
                "tokens_used": 4000,
                "duration_s": 120,
            }),
        )

        resume = generate_resume(ledger)

        assert "entry_summary" in resume, "entry_summary key missing"
        es = resume["entry_summary"]
        assert es is not None
        assert es["session_id"] == "sess-2", "debe ser el ÚLTIMO SESSION_SUMMARY"
        assert es["tool"] == "opencode"
        assert es["turn_count"] == 12
        assert es["summary_lines"] == ["Trabajamos en OCB ↔ BlobStore"]
        assert es["event_id"] is not None, "debe provenir del replay del ledger"

    def test_resume_entry_summary_none_without_summaries(self, tmp_path):
        """Fase 0 — sin SESSION_SUMMARY en el ledger → entry_summary None
        (no crashea)."""
        ledger = _make_workspace(tmp_path)
        _make_ocb_dir(ledger)
        active_path = os.path.join(os.path.dirname(ledger), "ocb", "OCB_ACTIVE.log")
        with open(active_path, "w") as f:
            f.write('{}\n')

        resume = generate_resume(ledger)
        assert "entry_summary" in resume
        assert resume["entry_summary"] is None


# ---------------------------------------------------------------------------
# R.4.0 — resume expone total_partitions + metadata de preloaded
# ---------------------------------------------------------------------------

def _write_partition_file(ocb_dir, ns: int, n_events: int = 5):
    """Escribe OCB_PARTITION_<ns>.log con n_events JSON lines que tienen
    timestamp (derivan first/last de la primera/última línea)."""
    fname = f"OCB_PARTITION_{ns}.log"
    with open(os.path.join(ocb_dir, fname), "w") as f:
        for i in range(n_events):
            ev = {
                "event_id": f"evt-{ns}-{i}",
                "event_type": "TOOL_CALLED",
                "ctx_id": "test",
                "source": "causadb:test",
                "source_type": "agent",
                "timestamp": f"2026-01-0{i + 1}T10:00:00.000000Z",
                "payload": {},
            }
            f.write(json.dumps(ev) + "\n")
    return fname


def _write_empty_partition_file(ocb_dir, ns):
    fname = f"OCB_PARTITION_{ns}.log"
    open(os.path.join(ocb_dir, fname), "w").close()
    return fname


class TestResumeOcbTotalPartitions:
    """R.4.0 — generate_resume expone total_partitions y preloaded_metadata."""

    def test_resume_first_run_exposes_total_partitions_zero(self, tmp_path):
        """workspace sin OCB ni ledger → generate_resume debe tener
        "total_partitions" presente y == 0."""
        ledger = _make_workspace(tmp_path)

        resume = generate_resume(ledger)

        assert "total_partitions" in resume
        assert resume["total_partitions"] == 0

    def test_resume_preloaded_metadata_empty_partition_uses_empty_strings(self, tmp_path):
        """Partición vacía → first_timestamp y last_timestamp deben ser "" (no None)."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)
        _write_empty_partition_file(ocb_dir, 1700000000000000000)

        resume = generate_resume(ledger)

        meta = resume.get("preloaded_metadata")
        assert meta[0]["event_count"] == 0
        assert meta[0]["first_timestamp"] == ""
        assert meta[0]["last_timestamp"] == ""

    def test_resume_preloaded_includes_metadata(self, tmp_path):
        """resume["preloaded_metadata"][0] tiene id, event_count,
        first_timestamp, last_timestamp (derivados de lines ya parseadas,
        sin resolver blobs).

        RED: hoy preloaded_metadata no existe → falla.
        GREEN: generate_resume lo deriva de ocb_ctx["preloaded_partitions"]."""
        ledger = _make_workspace(tmp_path)
        ocb_dir = _make_ocb_dir(ledger)
        _write_partition_file(ocb_dir, 1700000000000000000, 3)
        _write_partition_file(ocb_dir, 1700000000000000001, 5)

        resume = generate_resume(ledger)

        meta = resume.get("preloaded_metadata")
        assert meta, "preloaded_metadata debe estar presente con particiones"
        assert len(meta) == 2, f"esperaba 2 particiones preloaded, got {len(meta)}"
        for key in ("id", "event_count", "first_timestamp", "last_timestamp"):
            assert key in meta[0], f"preloaded_metadata[0] falta '{key}'"
        # preloaded sigue orden cronológico ascendente (antigua → reciente):
        # partición 001 es la reciente y tiene 5 eventos.
        recent = next(m for m in meta if "_1700000000000000001" in m["id"])
        assert recent["event_count"] == 5, (
            f"partición reciente debe tener 5 eventos, got {recent['event_count']}"
        )
        # first/last_timestamp derivados de las lines ya parseadas (sin
        # resolver blobs — Art. V).
        assert recent["first_timestamp"] == "2026-01-01T10:00:00.000000Z"
        assert recent["last_timestamp"] == "2026-01-05T10:00:00.000000Z"

    def test_resume_preloaded_metadata_empty_without_partitions(self, tmp_path):
        """Sin particiones → preloaded_metadata [] (no crashea).

        Mutación discriminatoria: si generate_resume se mutea a omitir la
        clave con OCB vacío, este test FALLA."""
        ledger = _make_workspace(tmp_path)
        _make_ocb_dir(ledger)

        resume = generate_resume(ledger)

        assert "preloaded_metadata" in resume, "clave preloaded_metadata ausente"
        assert resume["preloaded_metadata"] == []
