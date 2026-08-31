"""Tests for `causadb config mcp` — MCP configuration generation (one-shot).

Test-First (Artículo III): tests written BEFORE implementation.
Anti-teatro (Artículo IX): each test verifies exact format per tool.
"""
import json
import os
import sys

import pytest

from causadb.cli.main import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args, capsys):
    """Run the CLI with the given args list, return (exit_code, stdout_str)."""
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _init_workspace(tmp_path, capsys):
    """Initialize a workspace and return (workspace_path, ledger_path)."""
    ws = str(tmp_path / "ws")
    rc, out, err = _run(["init", ws], capsys)
    assert rc == 0
    payload = json.loads(out)
    return ws, payload["ledger_path"]


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------

def test_config_mcp_opencode_format(tmp_path, capsys):
    """`causadb config mcp --tool opencode` produces correct format."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)
    opencode_output = str(tmp_path / "causadb.opencode.jsonc")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "opencode",
        "--project", ws,
        "--output", opencode_output,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["output_path"] == opencode_output
    assert payload["ledger_path"] == ledger_path

    with open(opencode_output) as f:
        data = json.load(f)

    # Top-level must be "mcp" — NOT "mcpServers" (anti-teatro)
    assert "mcp" in data
    assert "mcpServers" not in data

    mcp = data["mcp"]["causadb"]
    assert mcp["type"] == "local"
    assert mcp["command"][0].endswith("causadb-mcp")
    assert len(mcp["command"]) == 1
    assert mcp["enabled"] is True
    assert "CAUSADB_LEDGER_PATH" in mcp["environment"]
    assert mcp["environment"]["CAUSADB_LEDGER_PATH"] == ledger_path
    assert mcp["environment"]["CAUSADB_WORKSPACE_DIR"] == str(tmp_path / "ws")

    # Anti-teatro: no claude-code fields
    assert "args" not in mcp
    assert "disabled" not in mcp
    assert "env" not in mcp
    assert "autoApprove" not in mcp


def test_config_mcp_opencode_writes_to_project_opencode_json(tmp_path, capsys):
    """Default (no --output): opencode must write to the file the client reads.

    This is a commercial-product requirement: the generated config must be
    picked up by opencode automatically, not a fragment the user must merge by
    hand. opencode only reads ``opencode.json``/``opencode.jsonc`` in the
    project root.
    """
    ws, ledger_path = _init_workspace(tmp_path, capsys)

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "opencode",
        "--project", ws,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)

    candidate = os.path.join(ws, "opencode.json")
    assert payload["output_path"] == candidate, \
        f"default output debe ser opencode.json, got {payload['output_path']}"
    assert os.path.exists(candidate), "opencode.json debe existir en el proyecto"

    with open(candidate) as f:
        data = json.load(f)

    # The client loads this file directly — top-level key must be "mcp"
    assert set(data.keys()) == {"mcp"}, \
        f"opencode.json solo debe contener el bloque mcp, got {set(data.keys())}"
    mcp = data["mcp"]["causadb"]
    assert mcp["type"] == "local"
    assert mcp["command"][0].endswith("causadb-mcp")
    assert mcp["enabled"] is True
    assert mcp["environment"]["CAUSADB_LEDGER_PATH"] == ledger_path


def test_config_mcp_opencode_merges_existing_project_config(tmp_path, capsys):
    """Der Wert present config must be preserved, not overwritten."""
    ws, _ = _init_workspace(tmp_path, capsys)
    existing = os.path.join(ws, "opencode.json")
    with open(existing, "w") as f:
        json.dump({
            "model": "anthropic/claude-sonnet-4-5",
            "mcp": {
                "github": {
                    "type": "local",
                    "command": ["gh-mcp"],
                    "enabled": True,
                },
            },
        }, f)

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "opencode",
        "--project", ws,
    ], capsys)

    assert rc == 0, out
    with open(existing) as f:
        data = json.load(f)

    # User's unrelated settings survive the merge
    assert data["model"] == "anthropic/claude-sonnet-4-5"
    assert data["mcp"]["github"]["command"] == ["gh-mcp"]

    # CausaDB block is added under mcp
    mcp = data["mcp"]["causadb"]
    assert mcp["type"] == "local"
    assert mcp["command"][0].endswith("causadb-mcp")


def test_config_mcp_opencode_rejects_existing_jsonc_without_modifying(tmp_path, capsys):
    """JSONC is never reserialized: manual migration is required."""
    ws, _ = _init_workspace(tmp_path, capsys)
    existing = os.path.join(ws, "opencode.jsonc")
    with open(existing, "w") as f:
        f.write('{\n  // user comment kept\n  "model": "deepseek",\n'
                '  "mcp": { "github": { "type": "local", "command": ["gh-mcp"] } }\n}\n')

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "opencode",
        "--project", ws,
    ], capsys)

    original = open(existing, "rb").read()
    assert rc != 0
    assert "JSONC" in json.loads(out)["error"]
    assert open(existing, "rb").read() == original


def test_config_mcp_codex_rejects_existing_toml_without_modifying(tmp_path, capsys):
    ws, _ = _init_workspace(tmp_path, capsys)
    existing = tmp_path / "config.toml"
    existing.write_bytes(b"# preserve me\n[other]\nvalue = 1\n")
    original = existing.read_bytes()
    rc, out, _ = _run(["config", "mcp", "--tool", "codex-cli",
                       "--project", ws, "--output", str(existing)], capsys)
    assert rc != 0
    assert "TOML" in json.loads(out)["error"]
    assert existing.read_bytes() == original


def test_config_mcp_write_is_atomic_and_cleans_temp(tmp_path, capsys, monkeypatch):
    ws, _ = _init_workspace(tmp_path, capsys)
    output = tmp_path / "config.json"
    real_replace = os.replace

    def fail_replace(src, dst):
        raise OSError("replace denied")

    monkeypatch.setattr(os, "replace", fail_replace)
    rc, out, _ = _run(["config", "mcp", "--tool", "cursor", "--project", ws,
                       "--output", str(output)], capsys)
    assert rc != 0
    assert "replace" in json.loads(out)["error"].lower()
    assert not output.exists()
    assert not list(tmp_path.glob(".config.json.*.tmp"))
    monkeypatch.setattr(os, "replace", real_replace)


def test_config_mcp_auto_preflights_all_destinations(tmp_path, capsys, monkeypatch):
    ws, _ = _init_workspace(tmp_path, capsys)
    existing_jsonc = tmp_path / "ws" / "opencode.jsonc"
    existing_jsonc.write_bytes(b'{\n // keep\n "model": "x"\n}\n')
    monkeypatch.setattr("causadb.cli._cmd_config_mcp._detect_installed_tools",
                        lambda: ["opencode", "cursor"])
    rc, out, _ = _run(["config", "mcp", "--auto", "--project", ws], capsys)
    assert rc != 0
    assert "JSONC" in json.loads(out)["error"]
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    assert existing_jsonc.read_bytes() == b'{\n // keep\n "model": "x"\n}\n'


def test_config_mcp_auto_never_writes_before_invalid_parent_is_rejected(
        tmp_path, capsys, monkeypatch):
    """A bad later parent must not leave an earlier auto destination behind."""
    ws, _ = _init_workspace(tmp_path, capsys)
    # The second destination's parent is a file, not a directory.  The first
    # destination must nevertheless remain untouched.
    bad_parent = tmp_path / "ws" / ".gemini"
    bad_parent.write_bytes(b"not a directory")
    monkeypatch.setattr("causadb.cli._cmd_config_mcp._detect_installed_tools",
                        lambda: ["cursor", "gemini-cli"])

    rc, out, _ = _run(["config", "mcp", "--auto", "--project", ws], capsys)

    assert rc != 0
    assert "directory" in json.loads(out)["error"].lower() or "file exists" in json.loads(out)["error"].lower()
    assert not (tmp_path / "ws" / ".cursor" / "mcp.json").exists()


def test_config_mcp_auto_rolls_back_previous_replaces(tmp_path, capsys, monkeypatch):
    """A failed later replace restores earlier files byte-for-byte."""
    ws, _ = _init_workspace(tmp_path, capsys)
    first = tmp_path / "ws" / ".cursor" / "mcp.json"
    first.parent.mkdir()
    original = b'{"user": "keep", "bytes": "exact"}\n'
    first.write_bytes(original)
    second = tmp_path / "ws" / ".gemini" / "settings.json"
    monkeypatch.setattr("causadb.cli._cmd_config_mcp._detect_installed_tools",
                        lambda: ["cursor", "gemini-cli"])
    real_replace = os.replace

    def fail_second_destination(src, dst):
        if os.path.abspath(dst) == os.path.abspath(second):
            raise OSError("replace denied for second destination")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second_destination)
    rc, out, _ = _run(["config", "mcp", "--auto", "--project", ws], capsys)

    assert rc != 0
    assert "replace denied" in json.loads(out)["error"]
    assert first.read_bytes() == original
    assert not second.exists()
    assert not list((tmp_path / "ws" / ".cursor").glob(".*.tmp"))
    assert not list((tmp_path / "ws" / ".gemini").glob(".*.tmp"))


# ---------------------------------------------------------------------------
# claude-code
# ---------------------------------------------------------------------------

def test_config_mcp_claude_code_format(tmp_path, capsys):
    """`causadb config mcp --tool claude-code` produces correct format."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)
    output_path = str(tmp_path / ".mcp.json")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "claude-code",
        "--project", ws,
        "--output", output_path,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["output_path"] == output_path
    assert payload["ledger_path"] == ledger_path

    with open(output_path) as f:
        data = json.load(f)

    # Top-level must be "mcpServers" — NOT "mcp" (anti-teatro)
    assert "mcpServers" in data
    assert "mcp" not in data

    sc = data["mcpServers"]["causadb"]
    # Command is string, args is list (opencode format is inverted)
    assert sc["command"].endswith("causadb-mcp")
    assert sc["args"] == []
    assert "env" in sc
    assert sc["env"]["CAUSADB_LEDGER_PATH"] == ledger_path
    assert sc["env"]["CAUSADB_WORKSPACE_DIR"] == str(tmp_path / "ws")

    # Anti-teatro: no opencode fields
    assert "enabled" not in sc
    assert "type" not in sc
    assert "command" not in sc or isinstance(sc["command"], str)


def test_config_mcp_claude_code_merges_existing_config(tmp_path, capsys):
    ws, _ = _init_workspace(tmp_path, capsys)
    output_path = tmp_path / ".mcp.json"
    existing = {
        "customSetting": {"keep": True},
        "mcpServers": {"github": {"command": "gh-mcp", "args": ["serve"]}},
    }
    output_path.write_text(json.dumps(existing))

    rc, out, _ = _run(["config", "mcp", "--tool", "claude-code",
                       "--project", ws, "--output", str(output_path)], capsys)

    assert rc == 0, out
    data = json.loads(output_path.read_text())
    assert data["customSetting"] == existing["customSetting"]
    assert data["mcpServers"]["github"] == existing["mcpServers"]["github"]
    assert data["mcpServers"]["causadb"]["command"].endswith("causadb-mcp")


# ---------------------------------------------------------------------------
# codex-cli
# ---------------------------------------------------------------------------

def test_config_mcp_codex_cli_format(tmp_path, capsys):
    """`causadb config mcp --tool codex-cli` produces TOML parseable."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)
    output_path = str(tmp_path / "config.toml")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "codex-cli",
        "--project", ws,
        "--output", output_path,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["output_path"] == output_path
    assert payload["ledger_path"] == ledger_path
    assert payload["tool"] == "codex-cli"

    # Parse TOML — tomllib available in Python 3.11+
    import tomllib
    with open(output_path, "rb") as f:
        parsed = tomllib.load(f)

    assert "mcp_servers" in parsed
    assert "causadb" in parsed["mcp_servers"]
    entry = parsed["mcp_servers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []
    assert entry["enabled"] is True

    # env is an inline table
    assert "env" in entry
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == ledger_path


# ---------------------------------------------------------------------------
# cursor
# ---------------------------------------------------------------------------

def test_config_mcp_cursor_format(tmp_path, capsys):
    """`causadb config mcp --tool cursor` produces correct format."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)
    output_path = str(tmp_path / ".cursor" / "mcp.json")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "cursor",
        "--project", ws,
        "--output", output_path,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["output_path"] == output_path
    assert payload["ledger_path"] == ledger_path

    with open(output_path) as f:
        data = json.load(f)

    assert "mcpServers" in data
    entry = data["mcpServers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []

    # Env must carry ledger + workspace dir (snapshots B.2)
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == ledger_path
    assert entry["env"]["CAUSADB_WORKSPACE_DIR"] == str(tmp_path / "ws")

    # Anti-teatro: cursor format does NOT include enabled or trust
    assert "enabled" not in entry
    assert "trust" not in entry


def test_config_mcp_cursor_merges_existing_config(tmp_path, capsys):
    ws, _ = _init_workspace(tmp_path, capsys)
    output_path = tmp_path / ".cursor" / "mcp.json"
    existing = {
        "editor": {"theme": "dark"},
        "mcpServers": {"filesystem": {"command": "fs-mcp"}},
    }
    output_path.parent.mkdir()
    output_path.write_text(json.dumps(existing))

    rc, out, _ = _run(["config", "mcp", "--tool", "cursor",
                       "--project", ws, "--output", str(output_path)], capsys)

    assert rc == 0, out
    data = json.loads(output_path.read_text())
    assert data["editor"] == existing["editor"]
    assert data["mcpServers"]["filesystem"] == existing["mcpServers"]["filesystem"]
    assert data["mcpServers"]["causadb"]["env"]["CAUSADB_LEDGER_PATH"]


# ---------------------------------------------------------------------------
# windsurf
# ---------------------------------------------------------------------------

def test_config_mcp_windsurf_format(tmp_path, capsys, monkeypatch):
    """`causadb config mcp --tool windsurf` writes globally and warns."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: str(fake_home / p.replace("~/", "")))

    ws, ledger_path = _init_workspace(tmp_path, capsys)

    rc = main(["config", "mcp", "--tool", "windsurf", "--project", ws])
    captured = capsys.readouterr()

    assert rc == 0, f"expected 0, got {rc}; stdout={captured.out!r}"
    payload = json.loads(captured.out)
    assert "ledger_path" in payload
    # Warn about --project being ignored
    assert "Warning" in captured.err

    # File at ~/.codeium/windsurf/mcp_config.json (fake home)
    expected_path = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    assert expected_path.exists(), f"Expected {expected_path} to exist"

    with open(expected_path) as f:
        data = json.load(f)
    assert "mcpServers" in data
    entry = data["mcpServers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []

    # Env must carry ledger + workspace dir (snapshots B.2)
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == ledger_path
    assert entry["env"]["CAUSADB_WORKSPACE_DIR"] == str(tmp_path / "ws")

    # Anti-teatro: windsurf has no enabled
    assert "enabled" not in entry


def test_config_mcp_windsurf_merges_global_config_and_ignores_destinations(
        tmp_path, capsys, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: str(fake_home / p.replace("~/", "")))
    ws, _ = _init_workspace(tmp_path, capsys)
    global_path = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    global_path.parent.mkdir(parents=True)
    existing = {
        "ui": {"fontSize": 14},
        "mcpServers": {"notion": {"command": "notion-mcp"}},
    }
    global_path.write_text(json.dumps(existing))
    ignored = tmp_path / "must-not-be-written.json"

    rc, out, _ = _run(["config", "mcp", "--tool", "windsurf",
                       "--project", ws,
                       "--output", str(ignored)], capsys)

    assert rc == 0, out
    data = json.loads(global_path.read_text())
    assert data["ui"] == existing["ui"]
    assert data["mcpServers"]["notion"] == existing["mcpServers"]["notion"]
    assert data["mcpServers"]["causadb"]["command"].endswith("causadb-mcp")
    assert not ignored.exists()


# ---------------------------------------------------------------------------
# gemini-cli
# ---------------------------------------------------------------------------

def test_config_mcp_gemini_cli_format(tmp_path, capsys):
    """`causadb config mcp --tool gemini-cli` produces correct format."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)
    output_path = str(tmp_path / ".gemini" / "settings.json")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "gemini-cli",
        "--project", ws,
        "--output", output_path,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["output_path"] == output_path
    assert payload["ledger_path"] == ledger_path

    with open(output_path) as f:
        data = json.load(f)

    assert "mcpServers" in data
    entry = data["mcpServers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []
    # Gemini-CLI has trust: true
    assert entry["trust"] is True
    # Env must carry both ledger path AND workspace dir (snapshots B.2)
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == ledger_path
    assert entry["env"]["CAUSADB_WORKSPACE_DIR"] == str(tmp_path / "ws")

    # Anti-teatro: no opencode fields, no claude-code env
    assert "enabled" not in entry
    assert "type" not in entry


def test_config_mcp_gemini_cli_merges_existing_config(tmp_path, capsys):
    ws, _ = _init_workspace(tmp_path, capsys)
    output_path = tmp_path / ".gemini" / "settings.json"
    existing = {
        "general": {"vimMode": True},
        "mcpServers": {"slack": {"command": "slack-mcp", "args": []}},
    }
    output_path.parent.mkdir()
    output_path.write_text(json.dumps(existing))

    rc, out, _ = _run(["config", "mcp", "--tool", "gemini-cli",
                       "--project", ws, "--output", str(output_path)], capsys)

    assert rc == 0, out
    data = json.loads(output_path.read_text())
    assert data["general"] == existing["general"]
    assert data["mcpServers"]["slack"] == existing["mcpServers"]["slack"]
    assert data["mcpServers"]["causadb"]["trust"] is True


# ---------------------------------------------------------------------------
# aider
# ---------------------------------------------------------------------------

def test_config_mcp_aider_message(tmp_path, capsys):
    """`causadb config mcp --tool aider` prints instructive message."""
    ws, ledger_path = _init_workspace(tmp_path, capsys)

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "aider",
        "--project", ws,
    ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "tool" in payload
    assert payload["tool"] == "aider"
    assert "message" in payload
    # Message must reference Aider
    assert "Aider" in payload["message"] or "aider" in payload["message"]
    # No file should have been created
    assert "output_path" not in payload


def test_config_mcp_auto_never_configures_out_of_scope(tmp_path, capsys, monkeypatch):
    ws, _ = _init_workspace(tmp_path, capsys)
    monkeypatch.setattr("causadb.cli._cmd_config_mcp._detect_installed_tools",
                        lambda: ["opencode", "grok", "aider"])
    rc, out, _ = _run(["config", "mcp", "--auto", "--project", ws], capsys)
    assert rc == 0
    payload = json.loads(out)
    assert payload["configured"] == ["opencode"]


# ---------------------------------------------------------------------------
# --auto
# ---------------------------------------------------------------------------

def test_config_mcp_auto(tmp_path, capsys, monkeypatch):
    """`causadb config mcp --auto` configures all detected tools."""
    from unittest.mock import patch

    ws, ledger_path = _init_workspace(tmp_path, capsys)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "missing"))

    # Mock which to return a path for specific tools
    def mock_which(name):
        if name in ("opencode", "claude", "cursor", "codex", "gemini", "aider"):
            return f"/usr/bin/{name}"
        return None

    with patch("shutil.which", side_effect=mock_which):
        rc, out, err = _run([
            "config", "mcp",
            "--auto",
            "--project", ws,
        ], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "configured" in payload
    assert "outputs" in payload
    assert "ledger_path" in payload
    assert payload["ledger_path"] == ledger_path

    configured = payload["configured"]
    assert "opencode" in configured
    assert "claude-code" in configured
    assert "cursor" in configured
    assert "codex-cli" in configured
    # windsurf NOT detected (no file exists in mocked env)
    assert "windsurf" not in configured

    # Verify output files actually exist
    for tool_name, output_file in payload["outputs"].items():
        assert os.path.exists(output_file), (
            f"Expected output file for {tool_name}: {output_file}"
        )


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_config_mcp_no_tool_no_auto(tmp_path, capsys):
    """`causadb config mcp` without --tool or --auto → error."""
    ws, _ = _init_workspace(tmp_path, capsys)

    rc, out, err = _run(["config", "mcp", "--project", ws], capsys)

    assert rc == 1, f"expected 1, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "error" in payload


def test_config_mcp_no_workspace(tmp_path, capsys):
    """`causadb config mcp` without .causadb/ → exit 1."""
    nowhere = str(tmp_path / "nonexistent")

    rc, out, err = _run([
        "config", "mcp",
        "--tool", "opencode",
        "--project", nowhere,
    ], capsys)

    assert rc == 1, f"expected 1, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "error" in payload
    assert ".causadb" in payload["error"] or "workspace" in payload["error"].lower()
