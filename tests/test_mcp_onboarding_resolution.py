"""Discriminating contract tests for MCP onboarding resolution.

These are intentionally written before the resolver implementation (RED phase).
They exercise the generated files and a real MCP subprocess, not a mocked server.
"""
import json
import os
import stat
import subprocess
import sys
import shutil
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from causadb._init import causadb_init
from causadb.cli.main import main
from causadb.cli._cmd_config_mcp import _resolve_mcp_launcher


TOOLS = {
    "opencode": "json",
    "claude-code": "json",
    "codex-cli": "toml",
    "cursor": "json",
    "windsurf": "json",
    "gemini-cli": "json",
    "aider": "message",
}


def _run(args, capsys):
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _workspace(tmp_path, capsys):
    project = str(tmp_path / "project")
    rc, out, _ = _run(["init", project], capsys)
    assert rc == 0, out
    return project, json.loads(out)["ledger_path"]


def _make_mcp_launcher(path, target="causadb.mcp.server:main"):
    path.write_text("#!/bin/sh\nexec python3 -c 'from causadb.mcp.server import main'\n# entry point " + target + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_all_templates_use_one_resolved_mcp_launcher(tmp_path, capsys, monkeypatch):
    project, _ = _workspace(tmp_path, capsys)
    launcher = tmp_path / "bin" / "causadb-mcp"
    launcher.parent.mkdir()
    _make_mcp_launcher(launcher)
    monkeypatch.setenv("PATH", str(launcher.parent))
    monkeypatch.setattr("shutil.which", lambda name: str(launcher) if name == "causadb-mcp" else None)

    for tool in TOOLS:
        if tool == "aider":
            continue
        output = tmp_path / tool / "config"
        if tool == "windsurf":
            monkeypatch.setattr("os.path.expanduser", lambda p: str(output))
        rc, out, _ = _run(["config", "mcp", "--tool", tool, "--project", project,
                           "--output", str(output)], capsys)
        assert rc == 0, out
        data = json.loads(out)
        assert output.exists()
        if tool == "opencode":
            generated = json.loads(output.read_text())["mcp"]["causadb"]
            assert generated["command"] == [str(launcher)]
        elif tool == "codex-cli":
            import tomllib
            generated = tomllib.loads(output.read_text())["mcp_servers"]["causadb"]
            assert generated["command"] == str(launcher)
            assert generated["args"] == []
        else:
            generated = json.loads(output.read_text())["mcpServers"]["causadb"]
            assert generated["command"] == str(launcher)
            assert generated["args"] == []


def test_resolver_finds_checkout_venv_when_path_is_absent(tmp_path, capsys, monkeypatch):
    project, _ = _workspace(tmp_path, capsys)
    launcher = tmp_path / "checkout with spaces" / ".venv" / "bin" / "causadb-mcp"
    launcher.parent.mkdir(parents=True)
    _make_mcp_launcher(launcher)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("causadb.cli._cmd_config_mcp.__file__", str(launcher.parent.parent.parent / "causadb" / "cli" / "_cmd_config_mcp.py"))
    # The implementation must discover an environment relative to CausaDB, not
    # rely on an operator-specific absolute path.
    rc, out, _ = _run(["config", "mcp", "--tool", "codex-cli", "--project", project,
                       "--output", str(tmp_path / "config.toml")], capsys)
    assert rc == 0, out


def test_invalid_or_non_executable_launcher_fails_without_partial_file(tmp_path, capsys, monkeypatch):
    project, _ = _workspace(tmp_path, capsys)
    launcher = tmp_path / "causadb-mcp"
    launcher.write_text("#!/bin/sh\n# not the MCP entrypoint\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: str(launcher) if name == "causadb-mcp" else None)
    output = tmp_path / "nested" / "config.json"
    rc, out, _ = _run(["config", "mcp", "--tool", "opencode", "--project", project,
                       "--output", str(output)], capsys)
    assert rc != 0
    assert "causadb-mcp" in json.loads(out)["error"]
    assert not output.exists()
    assert not output.parent.exists()


def test_launcher_text_only_is_rejected_as_non_mcp(tmp_path, monkeypatch):
    """A copied entrypoint comment must not qualify as a runnable server."""
    launcher = tmp_path / "causadb-mcp"
    launcher.write_text("#!/bin/sh\n# entry point causadb.mcp.server:main\nfrom causadb.mcp.server import main\nexit 0\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda name: str(launcher) if name == "causadb-mcp" else None)
    # The shell script contains the same entrypoint text but cannot execute the
    # Python MCP server.  Resolution must reject it without spawning a server.
    with pytest.raises(RuntimeError, match="not the CausaDB MCP entrypoint"):
        _resolve_mcp_launcher()


def test_real_handshake_uses_launcher_without_pythonpath(tmp_path, monkeypatch):
    # Direct init keeps this handshake test independent of CLI output capture.
    result = causadb_init(str(tmp_path / "project"), config=None)
    project, ledger = str(tmp_path / "project"), result["ledger_path"]
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    candidates = [
        Path(__file__).resolve().parents[1] / ".venv" / "bin" / "causadb-mcp",
        Path(sys.prefix) / "bin" / "causadb-mcp",
    ]
    path_launcher = shutil.which("causadb-mcp")
    if path_launcher:
        candidates.append(Path(path_launcher))
    launcher_path = next(
        (candidate for candidate in candidates
         if candidate.is_file() and os.access(candidate, os.X_OK)),
        None,
    )
    if launcher_path is None:
        pytest.skip("No real CausaDB causadb-mcp entrypoint is installed in this environment")

    async def scenario():
        params = StdioServerParameters(command=str(launcher_path), args=[], env={**env, "CAUSADB_LEDGER_PATH": ledger})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("replay", {"ledger_path": ledger})
                assert result.isError is False

    anyio.run(scenario)
