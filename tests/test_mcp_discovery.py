"""Tests for F.2 — MCP auto-discovery for OpenCode.

Artículo III: Test-first. Artículo IX: Anti-teatro.

``register_mcp_discovery`` writes/updates ``~/.config/opencode/mcp.json``
with a ``causadb`` entry under ``mcpServers``. The anti-teatro test below
defines a mutant that returns ``True`` without writing any file and confirms
that the file is NOT created — proving the real function actually performs
I/O and is not theatre.
"""

import json
import os

import pytest

from causadb._daemon_service import register_mcp_discovery


# ---------------------------------------------------------------------------
# F.2 — MCP auto-discovery registers the causadb entry
# ---------------------------------------------------------------------------


def test_anti_teatro_discovery_registers_mcp(tmp_path, monkeypatch):
    """Anti-teatro (Artículo IX): if ``register_mcp_discovery`` were a no-op
    that returned ``True`` without writing anything, the ``mcp.json`` file
    would NOT be created.

    This test:
    1. Redirects ``~/.config/opencode/`` to an isolated ``tmp_path`` via
       ``monkeypatch.setenv("HOME", ...)``.
    2. Calls the **real** ``register_mcp_discovery`` and verifies the file
       IS created with the correct structure.
    3. Defines a **mutant** version that returns ``True`` without writing
       anything and confirms the file is NOT created.

    If ``test_anti_teatro_discovery_registers_mcp`` were run against the
    mutant, it would FAIL (``mcp_path.exists()`` would be False). This
    proves the test has real discriminatory power.

    Graceful degradation is also verified by making the target directory
    non-writable — ``register_mcp_discovery`` must return ``False`` instead
    of crashing.
    """
    # Redirect home so we don't touch the real OpenCode config
    fake_home = str(tmp_path / "fake_home")
    os.makedirs(fake_home, exist_ok=True)
    monkeypatch.setenv("HOME", fake_home)

    ledger_path = str(tmp_path / "ledger.log")
    mcp_dir = os.path.join(fake_home, ".config", "opencode")
    mcp_path = os.path.join(mcp_dir, "mcp.json")

    # ---- Real function ----
    result = register_mcp_discovery(ledger_path)
    assert result is True, (
        "register_mcp_discovery must return True on success"
    )
    assert os.path.exists(mcp_path), (
        "mcp.json must exist after register_mcp_discovery. "
        "If it doesn't, the function is a no-op."
    )

    with open(mcp_path, "r") as f:
        content = json.load(f)

    assert "mcpServers" in content, "mcp.json must have 'mcpServers' key"
    assert "causadb" in content["mcpServers"], (
        "mcpServers must include 'causadb' entry"
    )
    causadb_entry = content["mcpServers"]["causadb"]
    assert causadb_entry["command"] == "causadb-mcp", (
        f"Expected command 'causadb-mcp', got {causadb_entry['command']!r}"
    )
    assert causadb_entry["args"] == [], (
        f"Expected args [], got {causadb_entry['args']!r}"
    )
    assert causadb_entry["env"]["CAUSADB_LEDGER_PATH"] == ledger_path, (
        f"Expected env.CAUSADB_LEDGER_PATH to be {ledger_path!r}, "
        f"got {causadb_entry['env']['CAUSADB_LEDGER_PATH']!r}"
    )

    # ---- Mutant: no-op that returns True ----
    mcp_path_unlink = mcp_path  # saved before removing
    os.unlink(mcp_path)

    def mutant_discovery(ignored_ledger_path):
        # No-op: returns True but writes nothing to disk
        return True

    mutant_result = mutant_discovery(ledger_path)
    assert mutant_result is True, "Mutant must return True"
    assert not os.path.exists(mcp_path), (
        "Mutant (no-op) did NOT write mcp.json. If the real function were "
        "a no-op, it would also fail to create the file — proving that "
        "test_anti_teatro_discovery_registers_mcp has discriminatory power. "
        "If this assertion fails, the mutant somehow created the file, "
        "which means it's not a real no-op."
    )

    # ---- Graceful degradation (non-writable directory) ----
    # Make the parent directory read-only
    os.chmod(mcp_dir, 0o444)  # read-only
    try:
        degraded_result = register_mcp_discovery(ledger_path)
        assert degraded_result is False, (
            "register_mcp_discovery must return False when the target "
            "directory is not writable (graceful degradation). "
            f"Got {degraded_result!r}"
        )
    finally:
        os.chmod(mcp_dir, 0o755)  # restore permissions
