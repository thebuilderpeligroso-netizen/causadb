"""Tests for F.11.3 — Git hook COMMIT_MADE automático (_git_hook.py).

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import os
import stat
import tempfile

import pytest

from causadb._git_hook import install_post_commit_hook, git_dir_from_workspace


# ---------------------------------------------------------------------------
# install_post_commit_hook
# ---------------------------------------------------------------------------


def _make_git_dir(root: str) -> str:
    """Create a minimal .git/ directory (just hooks dir)."""
    hooks = os.path.join(root, ".git", "hooks")
    os.makedirs(hooks)
    return hooks


def test_git_hook_installs_post_commit():
    with tempfile.TemporaryDirectory() as tmp:
        hooks = _make_git_dir(tmp)
        result = install_post_commit_hook(tmp, "/tmp/.causadb/ledger.log")
        assert result is True
        hook_path = os.path.join(hooks, "post-commit")
        assert os.path.exists(hook_path)
        mode = os.stat(hook_path).st_mode
        assert mode & stat.S_IXUSR or mode & stat.S_IXGRP or mode & stat.S_IXOTH


def test_git_hook_does_not_overwrite_existing():
    with tempfile.TemporaryDirectory() as tmp:
        hooks = _make_git_dir(tmp)
        hook_path = os.path.join(hooks, "post-commit")
        with open(hook_path, "w") as f:
            f.write("#!/bin/bash\necho existing\n")
        result = install_post_commit_hook(tmp, "/tmp/.causadb/ledger.log")
        assert result is False
        with open(hook_path) as f:
            content = f.read()
        assert "existing" in content


def test_git_hook_content_has_ledger_path():
    with tempfile.TemporaryDirectory() as tmp:
        hooks = _make_git_dir(tmp)
        ledger = "/my/project/.causadb/ledger.log"
        install_post_commit_hook(tmp, ledger)
        hook_path = os.path.join(hooks, "post-commit")
        with open(hook_path) as f:
            content = f.read()
        assert ledger in content


def test_git_hook_content_has_correct_event_type():
    with tempfile.TemporaryDirectory() as tmp:
        hooks = _make_git_dir(tmp)
        install_post_commit_hook(tmp, "/tmp/.causadb/ledger.log")
        hook_path = os.path.join(hooks, "post-commit")
        with open(hook_path) as f:
            content = f.read()
        assert "COMMIT_MADE" in content


# ---------------------------------------------------------------------------
# git_dir_from_workspace
# ---------------------------------------------------------------------------


def test_git_dir_from_workspace_finds_git():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, ".git"))
        sub = os.path.join(tmp, "src", "causadb")
        os.makedirs(sub)
        config_path = os.path.join(sub, ".causadb", "config.json")
        os.makedirs(os.path.dirname(config_path))
        found = git_dir_from_workspace(config_path)
        assert found == tmp


def test_git_dir_from_workspace_no_git():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, ".causadb", "config.json")
        os.makedirs(os.path.dirname(config_path))
        found = git_dir_from_workspace(config_path)
        assert found is None


# ---------------------------------------------------------------------------
# Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_hook_does_not_call_log():
    """Verify hook content actually invokes causadb log, not just echo."""
    with tempfile.TemporaryDirectory() as tmp:
        hooks = _make_git_dir(tmp)
        install_post_commit_hook(tmp, "/tmp/.causadb/ledger.log")
        hook_path = os.path.join(hooks, "post-commit")
        with open(hook_path) as f:
            content = f.read()
        assert "causadb.cli.main" in content or "causadb log" in content
        assert "echo" not in content.split("\n")[1]  # first non-shebang line must not be echo
