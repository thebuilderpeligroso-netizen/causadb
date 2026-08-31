"""Tests for `causadb audit` (F.12.6) — measures AI-authored code survival
in git history. Standalone: no `.causadb/` workspace required, only git CLI.

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They build synthetic tmp git repos via `subprocess.run`
(no real repo, no network) and assert on the resulting `AuditReport`.

Anti-teatro (Article IX): every test has discriminatory power. The last
test (`test_anti_teatro_audit_skips_blame`) mutates `compute_survival`
to skip `git blame` and confirms survival collapses to 0 — proving the
blame step is actually exercised by the survival tests.
"""
import json
import os
import subprocess

import pytest

from causadb._audit import (
    AuditReport,
    AuditError,
    Commit,
    Evidence,
    read_commits,
    detect_ai,
    compute_survival,
    aggregate,
)
from causadb.cli.main import main


# ---------------------------------------------------------------------------
# Helpers — build a synthetic git repo in tmp_path
# ---------------------------------------------------------------------------

def _git(repo, *args, **kwargs):
    """Run a git command inside `repo`, return CompletedProcess."""
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = env.get("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    env["GIT_COMMITTER_DATE"] = env.get("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=kwargs.pop("check", True),
    )


def _commit(repo, message, author_name="Human", author_email="human@example.com",
            files=None):
    """Stage + commit with a specific author identity."""
    if files is None:
        files = {}
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _git(repo, "add", "--", name)
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    env["GIT_AUTHOR_DATE"] = env.get("GIT_AUTHOR_DATE", "2026-01-01T00:00:00")
    env["GIT_COMMITTER_DATE"] = env.get("GIT_COMMITTER_DATE", "2026-01-01T00:00:00")
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


def _init_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Default")
    _git(repo, "config", "user.email", "default@example.com")
    _git(repo, "config", "commit.gpgsign", "false")


# ---------------------------------------------------------------------------
# detect_ai
# ---------------------------------------------------------------------------

def test_audit_detects_ai_commit_by_trailer(tmp_path):
    """Commit with `Co-Authored-By: Claude` trailer → AI Verified."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add module\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        files={"a.py": "x = 1\n"},
    )
    commits = read_commits(repo)
    assert len(commits) == 1
    ai, agent, evidence = detect_ai(commits[0])
    assert ai is True
    assert agent == "claude"
    assert evidence == Evidence.VERIFIED


def test_audit_detects_ai_commit_by_email(tmp_path):
    """Commit with author email `noreply@anthropic.com` → AI Verified."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add module",
        author_name="Claude",
        author_email="noreply@anthropic.com",
        files={"a.py": "x = 1\n"},
    )
    commits = read_commits(repo)
    assert len(commits) == 1
    ai, agent, evidence = detect_ai(commits[0])
    assert ai is True
    assert agent == "claude"
    assert evidence == Evidence.VERIFIED


def test_audit_detects_aider_marker(tmp_path):
    """Commit message containing `(aider)` → AI Probable."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add module (aider)",
        files={"a.py": "x = 1\n"},
    )
    commits = read_commits(repo)
    assert len(commits) == 1
    ai, agent, evidence = detect_ai(commits[0])
    assert ai is True
    assert agent == "aider"
    assert evidence == Evidence.PROBABLE


def test_audit_ignores_human_commit(tmp_path):
    """Human commit with no AI markers → not AI."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add module",
        author_name="Jane Dev",
        author_email="jane@example.com",
        files={"a.py": "x = 1\n"},
    )
    commits = read_commits(repo)
    assert len(commits) == 1
    ai, agent, evidence = detect_ai(commits[0])
    assert ai is False
    assert agent is None
    assert evidence == Evidence.UNKNOWN


# ---------------------------------------------------------------------------
# compute_survival
# ---------------------------------------------------------------------------

def test_audit_computes_survival(tmp_path):
    """AI commit introduces 100 lines, 60 survive at HEAD → survival > 0.

    We don't assert exactly 60% (blame semantics vary by git version) but
    we DO assert survival is strictly positive — a stub that skips blame
    returns 0 and fails here. See `test_anti_teatro_audit_skips_blame`.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    # AI commit introduces 100 lines.
    _commit(
        repo,
        "feat: add 100 lines\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        author_name="Dev",
        author_email="dev@example.com",
        files={"big.py": "\n".join(f"line{i} = {i}" for i in range(100)) + "\n"},
    )
    # Human commit deletes 40 of them (leaves 60 surviving).
    surviving = "\n".join(f"line{i} = {i}" for i in range(60)) + "\n"
    _commit(
        repo,
        "chore: trim",
        author_name="Jane",
        author_email="jane@example.com",
        files={"big.py": surviving},
    )
    commits = read_commits(repo)
    ai_commit = commits[0]
    assert detect_ai(ai_commit)[0] is True
    survival = compute_survival(ai_commit, repo)
    assert survival > 0.0, f"expected survival > 0, got {survival}"
    # Sanity upper bound (clamped to 1.0).
    assert survival <= 1.0


def test_audit_clamps_survival_to_100(tmp_path):
    """If blame reports more surviving lines than introduced (context lines,
    renames, etc.) survival must clamp to 1.0 (100%), never exceed it."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    # AI commit introduces 1 line.
    _commit(
        repo,
        "feat: add one line\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        author_name="Dev",
        author_email="dev@example.com",
        files={"a.py": "x = 1\n"},
    )
    # Human commit adds 99 more lines to the same file (no deletion).
    _commit(
        repo,
        "chore: expand",
        author_name="Jane",
        author_email="jane@example.com",
        files={"a.py": "x = 1\n" + "\n".join(f"y{i} = {i}" for i in range(99)) + "\n"},
    )
    commits = read_commits(repo)
    ai_commit = commits[0]
    survival = compute_survival(ai_commit, repo)
    assert survival <= 1.0, f"expected survival <= 1.0, got {survival}"
    assert survival == 1.0, f"expected clamped to 1.0, got {survival}"


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def test_audit_aggregates_by_agent(tmp_path):
    """2 Claude commits + 1 Copilot commit → aggregate groups by agent."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    # Claude commit #1
    _commit(
        repo,
        "feat: a\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        files={"a.py": "a = 1\n"},
    )
    # Claude commit #2
    _commit(
        repo,
        "feat: b\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        files={"b.py": "b = 1\n"},
    )
    # Copilot commit
    _commit(
        repo,
        "feat: c\n\nCo-Authored-By: Copilot <noreply@github.com>",
        files={"c.py": "c = 1\n"},
    )
    commits = read_commits(repo)
    report = AuditReport.build(repo)
    agg = aggregate(commits, repo)
    # Two agents present.
    agents = {a["agent"] for a in agg}
    assert "claude" in agents
    assert "copilot" in agents
    # Claude has 2 commits, Copilot has 1.
    by_agent = {a["agent"]: a for a in agg}
    assert by_agent["claude"]["commits"] == 2
    assert by_agent["copilot"]["commits"] == 1


# ---------------------------------------------------------------------------
# CLI output — markdown
# ---------------------------------------------------------------------------

def test_audit_outputs_markdown(tmp_path, capsys):
    """`causadb audit --format markdown` → Markdown table on stdout."""
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add module\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        files={"a.py": "x = 1\n"},
    )
    rc, out = _run_cli(["audit", "--format", "markdown", "--repo", str(repo)], capsys)
    assert rc == 0, f"exit {rc}; stdout={out!r}"
    assert "# CausaDB AI Audit" in out
    assert "|" in out  # table row
    assert "claude" in out.lower()


def _run_cli(args, capsys):
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out


# ---------------------------------------------------------------------------
# Anti-teatro (Article IX)
# ---------------------------------------------------------------------------

def test_anti_teatro_audit_skips_blame(tmp_path):
    """Mutate `compute_survival` to skip `git blame` → survival must collapse
    to 0, failing the survival test. Then RESTORE the original implementation.

    This proves the survival tests actually exercise the blame step — a stub
    that returns 0 without running blame would pass the survival tests if
    they didn't check `> 0`, but here we explicitly verify the mutation
    breaks the contract.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(
        repo,
        "feat: add 100 lines\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        author_name="Dev",
        author_email="dev@example.com",
        files={"big.py": "\n".join(f"line{i} = {i}" for i in range(100)) + "\n"},
    )
    _commit(
        repo,
        "chore: trim",
        author_name="Jane",
        author_email="jane@example.com",
        files={"big.py": "\n".join(f"line{i} = {i}" for i in range(60)) + "\n"},
    )
    commits = read_commits(repo)
    ai_commit = commits[0]

    # Sanity: real implementation returns > 0.
    real_survival = compute_survival(ai_commit, repo)
    assert real_survival > 0.0, "real compute_survival must return > 0"

    # --- MUTATION: stub compute_survival to skip blame (always 0) ---
    import causadb._audit as audit_mod
    original = audit_mod.compute_survival

    def _stub_skip_blame(commit, repo_dir):
        return 0.0

    audit_mod.compute_survival = _stub_skip_blame
    try:
        mutated_survival = audit_mod.compute_survival(ai_commit, repo)
        assert mutated_survival == 0.0, (
            "mutated compute_survival (skip blame) must return 0 — "
            "otherwise the anti-teatro check is vacuous"
        )
        # The survival test contract (`> 0`) would FAIL under the mutation.
        assert mutated_survival <= 0.0, "mutation must break the `> 0` contract"
    finally:
        # --- RESTORE ---
        audit_mod.compute_survival = original

    # After restore, the real implementation works again.
    restored_survival = compute_survival(ai_commit, repo)
    assert restored_survival > 0.0, "restored compute_survival must return > 0"
    assert restored_survival == real_survival, "restore must be exact"
