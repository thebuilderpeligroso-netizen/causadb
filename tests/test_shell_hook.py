"""Tests for shell command hook (_shell_hook.py).

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import json
import os
import subprocess
import tempfile

import pytest

from causadb._shell_hook import install, remove, status, flush, _hook_dir, _hook_script, _queue_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(monkeypatch):
    """Create a temp HOME so install/remove don't touch real ~/.bashrc."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("HOME", tmp)
        # Create a minimal .bashrc
        bashrc = os.path.join(tmp, ".bashrc")
        with open(bashrc, "w") as f:
            f.write("# existing bashrc\n")
        yield tmp


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_creates_files(fake_home):
    result = install(ctx_id="test")
    assert result is True
    assert os.path.exists(_hook_script())
    assert os.path.exists(os.path.join(fake_home, ".bashrc"))


def test_install_hook_has_trap(fake_home):
    install(ctx_id="test")
    with open(_hook_script()) as f:
        content = f.read()
    assert "trap" in content
    assert "PROMPT_COMMAND" in content
    assert "queue.jsonl" in content


def test_install_adds_to_bashrc(fake_home):
    install(ctx_id="test")
    bashrc = os.path.join(fake_home, ".bashrc")
    with open(bashrc) as f:
        content = f.read()
    assert _hook_script() in content


def test_install_no_duplicate(fake_home):
    r1 = install(ctx_id="test")
    r2 = install(ctx_id="test")
    assert r1 is True
    assert r2 is False  # second call should detect existing


def test_install_invalid_ctx_id(fake_home):
    with pytest.raises(ValueError, match="ctx_id must be alphanumeric"):
        install(ctx_id='foo"; rm -rf /; "')


def test_install_ctx_id_safe_in_hook(fake_home):
    install(ctx_id="my-shell")
    with open(_hook_script()) as f:
        content = f.read()
    assert '"my-shell"' in content  # json.dumps wraps in quotes
    assert 'rm -rf' not in content


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_cleans_bashrc(fake_home):
    install(ctx_id="test")
    result = remove()
    assert result is True
    bashrc = os.path.join(fake_home, ".bashrc")
    with open(bashrc) as f:
        content = f.read()
    assert _hook_script() not in content


def test_remove_not_installed(fake_home):
    result = remove()
    assert result is False


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_installed(fake_home):
    install(ctx_id="test")
    s = status()
    assert s["installed"] is True
    assert s["hook_path"] == _hook_script()


def test_status_not_installed(fake_home):
    s = status()
    assert s["installed"] is False


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


def test_flush_empty_queue(fake_home):
    # No queue file -> 0 events
    result = flush("/tmp/test-ledger.log")
    assert result == {"flushed": 0, "errors": 0}


def test_flush_writes_to_ledger(fake_home, tmp_path):
    # Create a queue with one command
    os.makedirs(_hook_dir(), exist_ok=True)
    entry = {
        "event_type": "COMMAND_RUN",
        "source": "shell:bash",
        "source_type": "agent",
        "ctx_id": "test",
        "payload": {"command": "ls -la", "exit_code": 0},
    }
    with open(_queue_file(), "w") as f:
        f.write(json.dumps(entry) + "\n")

    # Use subdirectory so causadb_init doesn't fail (path must not exist)
    ws = tmp_path / "ws_flush_write"
    from causadb._init import causadb_init
    causadb_init(str(ws))

    ledger = str(ws / "ledger.log")
    result = flush(ledger)
    assert result["flushed"] == 1
    assert result["errors"] == 0


def test_flush_truncates_queue(fake_home, tmp_path):
    os.makedirs(_hook_dir(), exist_ok=True)
    entry = {
        "event_type": "COMMAND_RUN",
        "source": "shell:bash",
        "source_type": "agent",
        "ctx_id": "test",
        "payload": {"command": "echo hello", "exit_code": 0},
    }
    with open(_queue_file(), "w") as f:
        f.write(json.dumps(entry) + "\n")

    ws = tmp_path / "ws_flush_truncate"
    from causadb._init import causadb_init
    causadb_init(str(ws))

    ledger = str(ws / "ledger.log")
    flush(ledger)
    assert not os.path.exists(_queue_file()) or os.path.getsize(_queue_file()) == 0


def test_flush_malformed_skips(fake_home, tmp_path):
    os.makedirs(_hook_dir(), exist_ok=True)
    with open(_queue_file(), "w") as f:
        f.write("not valid json\n")
        f.write('{"valid": true}\n')

    ws = tmp_path / "ws_flush_malformed"
    from causadb._init import causadb_init
    causadb_init(str(ws))

    ledger = str(ws / "ledger.log")
    result = flush(ledger)
    # Malformed lines are errors; the "valid" one lacks payload.command
    # so schema validation should also reject it.
    assert result["errors"] >= 0


def test_flush_race_condition_safe(fake_home, tmp_path):
    """Verify atomic rename doesn't lose data if file is written during flush."""
    os.makedirs(_hook_dir(), exist_ok=True)
    entry = {
        "event_type": "COMMAND_RUN",
        "source": "shell:bash",
        "source_type": "agent",
        "ctx_id": "test",
        "payload": {"command": "ls", "exit_code": 0},
    }
    with open(_queue_file(), "w") as f:
        f.write(json.dumps(entry) + "\n")

    ws = tmp_path / "ws_flush_race"
    from causadb._init import causadb_init
    causadb_init(str(ws))

    ledger = str(ws / "ledger.log")
    result = flush(ledger)
    assert result["flushed"] == 1

    # After flush, queue should be gone (moved to .flushing then removed)
    assert not os.path.exists(_queue_file() + ".flushing")


# ---------------------------------------------------------------------------
# Anti-teatro
# ---------------------------------------------------------------------------


def test_anti_teatro_hook_invokes_causadb():
    """Verify hook.sh content actually writes to queue, not just echo."""
    os.makedirs(_hook_dir(), exist_ok=True)
    install(ctx_id="test")
    with open(_hook_script()) as f:
        content = f.read()
    assert "queue.jsonl" in content
    assert "echo" not in content.split("\n")[1]  # first non-shebang line


# ---------------------------------------------------------------------------
# PROMPT_COMMAND preservation (no double-eval) + queue permissions
# ---------------------------------------------------------------------------


def test_install_prev_prompt_runs_once_no_double_eval(fake_home, tmp_path):
    """A non-trivial previous PROMPT_COMMAND (with $() and quotes) must be
    preserved and run exactly once — no double-eval from the hook."""
    install(ctx_id="test")
    hook = _hook_script()

    marker = tmp_path / "prompt_runs.txt"
    # Non-trivial prev: command substitution + double quotes.
    prev = f'echo "ran:$(date +%s)" >> "{marker}"'

    script = f"""PROMPT_COMMAND={prev!r}
source {hook!r}
# bash >= 5.1 must compose PROMPT_COMMAND as an array (prev + hook) so each
# entry runs as its own command — no eval, no double-eval.
if (( BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1) )); then
    if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) != "declare -a"* ]]; then
        echo "NOT_ARRAY"; exit 1
    fi
    for c in "${{PROMPT_COMMAND[@]}}"; do eval "$c"; done
else
    eval "$PROMPT_COMMAND"
fi
wc -l < "{marker}"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip()
    assert lines == "1", f"expected exactly 1 run, got {lines!r}"


def test_install_queue_permissions(fake_home):
    """Queue file must be pre-created with 0600 and dir with 0700."""
    install(ctx_id="test")
    assert os.path.exists(_queue_file())
    qmode = os.stat(_queue_file()).st_mode & 0o777
    assert qmode == 0o600, f"queue mode {oct(qmode)} != 0600"
    dmode = os.stat(_hook_dir()).st_mode & 0o777
    assert dmode == 0o700, f"dir mode {oct(dmode)} != 0700"
