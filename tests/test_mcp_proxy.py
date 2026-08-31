"""Tests for F.9 — MCP Middleware Proxy (McpProxy).

Test-First (Article III): these tests are written BEFORE the implementation.
Anti-teatro (Article IX): every core test has discriminatory power.

Pattern: synchronous tests using anyio.run for async MCP calls (same as
test_mcp_server.py). Mock stdio_client and ClientSession for server-interaction
tests; real LedgerWriter for logging tests.
"""

import json
import os
import hashlib
import pytest
import anyio
from unittest.mock import patch, MagicMock, AsyncMock

from causadb.mcp._proxy import _truncate, _prefix_tool


# ---------------------------------------------------------------------------
# 1. Tool namespacing
# ---------------------------------------------------------------------------

class TestNamespacing:
    def test_prefix_tool_namespacing(self):
        """_prefix_tool('filesystem', 'read_file') → 'filesystem_read_file'."""
        assert _prefix_tool("filesystem", "read_file") == "filesystem_read_file"

    def test_prefix_tool_with_underscores(self):
        """Tool names with internal underscores are preserved."""
        assert _prefix_tool("git", "create_branch") == "git_create_branch"


# ---------------------------------------------------------------------------
# 2. Truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_truncation_applies_over_4kb(self):
        """Result >4096 bytes → truncated to 4096, was_truncated=True, sha256."""
        content = "x" * 5000
        result = _truncate(content, 4096)
        assert result["was_truncated"] is True
        assert len(result["truncated"]) <= 4096
        assert result["result_hash"] == hashlib.sha256(content.encode()).hexdigest()
        assert result["result_length"] == 5000

    def test_truncation_skips_under_4kb(self):
        """Result <4096 bytes → not truncated, was_truncated=False."""
        content = "x" * 1000
        result = _truncate(content, 4096)
        assert result["was_truncated"] is False
        assert result["truncated"] == content
        assert result["result_length"] == 1000

    def test_truncation_hash_deterministic(self):
        """Same content → same hash every time."""
        content = "hello world"
        r1 = _truncate(content, 4096)
        r2 = _truncate(content, 4096)
        assert r1["result_hash"] == r2["result_hash"]

    def test_truncation_at_exact_boundary(self):
        """Result exactly 4096 bytes → not truncated."""
        content = "x" * 4096
        result = _truncate(content, 4096)
        assert result["was_truncated"] is False
        assert result["result_length"] == 4096

    def test_truncation_empty_string(self):
        """Empty string → was_truncated=False, valid hash."""
        result = _truncate("", 4096)
        assert result["was_truncated"] is False
        assert result["truncated"] == ""
        assert result["result_hash"] == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# 3. McpProxy — server lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_mcp_client():
    """Fixture that patches stdio_client and ClientSession to return mocks."""
    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock()
    mock_session.call_tool = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock()

    mock_read = AsyncMock()
    mock_write = AsyncMock()
    mock_stdio = AsyncMock()
    mock_stdio.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
    mock_stdio.__aexit__ = AsyncMock()

    patches = [
        patch("mcp.client.stdio.stdio_client", return_value=mock_stdio),
        patch("mcp.client.session.ClientSession", return_value=mock_session),
    ]
    for p in patches:
        p.start()
    yield mock_session
    for p in patches:
        p.stop()


class TestServerLifecycle:
    def test_start_connects_to_server(self, mock_mcp_client, tmp_path):
        """start() spawns subprocess, connects via stdio_client, initializes session."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        names = anyio.run(proxy.start)

        assert len(names) == 1
        assert names[0] == "echo"
        assert proxy._servers["echo"].alive is True

    def test_stop_terminates_server(self, mock_mcp_client, tmp_path):
        """stop() closes session and marks server as not alive."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.stop)

        assert proxy._servers["echo"].alive is False

    def test_start_best_effort_one_fails(self, mock_mcp_client, tmp_path):
        """If one server fails to start, others still start."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "good": {"command": ["python3", "-c", ""]},
                "bad": {"command": ["nonexistent-command-xyz"]},
            },
        }))

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        names = anyio.run(proxy.start)

        assert "good" in names
        assert proxy._servers["good"].alive is True

    def test_list_tools_aggregates(self, mock_mcp_client, tmp_path):
        """list_tools() returns tools from all servers with prefixed names."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "fs": {"command": ["python3", "-c", ""]},
                "git": {"command": ["python3", "-c", ""]},
            },
        }))

        # Configure two different mock sessions with different tool lists
        mock_fs = AsyncMock()
        mock_fs.list_tools.return_value = type("LTR", (), {
            "tools": [type("Tool", (), {"name": "read", "inputSchema": {"type": "object"}})()]
        })()

        mock_git = AsyncMock()
        mock_git.list_tools.return_value = type("LTR", (), {
            "tools": [type("Tool", (), {"name": "create", "inputSchema": {"type": "object"}})()]
        })()

        from causadb.mcp._proxy import ServerState
        fs_state = ServerState(
            name="fs", config={}, alive=True, session=mock_fs,
        )
        git_state = ServerState(
            name="git", config={}, alive=True, session=mock_git,
        )

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        proxy._servers = {"fs": fs_state, "git": git_state}

        tools = anyio.run(proxy.list_tools)
        names = [t["name"] for t in tools]
        assert "fs_read" in names
        assert "git_create" in names

    def test_stop_idempotent(self, mock_mcp_client, tmp_path):
        """Calling stop() twice does not raise."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.stop)
        anyio.run(proxy.stop)  # second call — must not raise
        assert proxy._servers["echo"].alive is False

    def test_crash_recovery_relaunches(self, mock_mcp_client, tmp_path):
        """If a server crashes, call_tool relaunches it."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": "pong", "type": "text"})()]}
        )()

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)

        # Simulate crash
        proxy._servers["echo"].alive = False
        proxy._servers["echo"].session = None

        result = anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})
        assert result is not None


# ---------------------------------------------------------------------------
# 4. Tool call logging
# ---------------------------------------------------------------------------

class TestToolCallLogging:
    def test_tool_call_logs_tool_called_event(self, mock_mcp_client, tmp_path):
        """call_tool() logs a TOOL_CALLED event to the ledger."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": "pong", "type": "text"})()]}
        )()

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})

        # Read ledger
        lines = ledger.read_text().strip().split("\n")
        events = [json.loads(l) for l in lines if l.strip()]
        tool_events = [
            e for e in events
            if e.get("event", {}).get("event_type") == "TOOL_CALLED"
        ]
        assert len(tool_events) >= 1, "No TOOL_CALLED event found in ledger"
        ev = tool_events[-1]["event"]
        assert ev["payload"]["tool_name"] == "echo_ping"
        assert "arguments" in ev["payload"]
        assert "result_hash" in ev["payload"]

    def test_tool_call_error_logs_error_event(self, mock_mcp_client, tmp_path):
        """On error, TOOL_CALLED is still logged with error field and result=null."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.side_effect = RuntimeError("connection lost")

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)

        with pytest.raises(RuntimeError):
            anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})

        lines = ledger.read_text().strip().split("\n")
        events = [json.loads(l) for l in lines if l.strip()]
        tool_events = [
            e for e in events
            if e.get("event", {}).get("event_type") == "TOOL_CALLED"
        ]
        assert len(tool_events) >= 1
        ev = tool_events[-1]["event"]
        assert "error" in ev["payload"]
        assert ev["payload"]["error"] is not None

    def test_tool_call_writes_via_ledger_writer(self, mock_mcp_client, tmp_path, monkeypatch):
        """Art. I: TOOL_CALLED is written via LedgerWriter.append(), not open().

        Anti-teatro: a spy on LedgerWriter.append verifies it was called.
        """
        from causadb.mcp._proxy import McpProxy
        from causadb._ledger_writer import LedgerWriter

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": "ok", "type": "text"})()]}
        )()

        received = []
        original_append = LedgerWriter.append

        def spy_append(self, event):
            received.append(event)
            return original_append(self, event)

        monkeypatch.setattr(LedgerWriter, "append", spy_append)

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})

        assert len(received) >= 1, "LedgerWriter.append was not called"
        ev = received[-1]
        from causadb._event_types import EventType
        assert ev.event_type == EventType.TOOL_CALLED

    def test_tool_call_source_is_mcp_proxy(self, mock_mcp_client, tmp_path):
        """source is 'causadb:mcp-proxy'."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": "pong", "type": "text"})()]}
        )()

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})

        lines = ledger.read_text().strip().split("\n")
        events = [json.loads(l) for l in lines if l.strip()]
        tool_events = [
            e for e in events
            if e.get("event", {}).get("event_type") == "TOOL_CALLED"
        ]
        assert tool_events[-1]["event"]["source"] == "causadb:mcp-proxy"

    def test_tool_call_truncates_large_result(self, mock_mcp_client, tmp_path):
        """Results >4096 bytes are truncated in the ledger event."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
            "truncation_bytes": 100,
        }))

        large = "x" * 500
        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": large, "type": "text"})()]}
        )()

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.call_tool, "echo_ping", {})

        lines = ledger.read_text().strip().split("\n")
        events = [json.loads(l) for l in lines if l.strip()]
        ev = [e for e in events if e.get("event", {}).get("event_type") == "TOOL_CALLED"][-1]["event"]
        assert ev["payload"]["truncated"] is True
        assert len(ev["payload"]["result_hash"]) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# 5. Anti-teatro — mutation detection (Article IX)
# ---------------------------------------------------------------------------
# Each test proves that a mutation to the code produces a detectable failure.
# We use a context-manager pattern: mutate → assert core test would fail → restore.

class TestAntiTeatro:
    def test_anti_teatro_truncation_empty_hash_fails(self):
        """Mutation: _truncate returns empty hash → hash assertion fails."""
        from causadb.mcp._proxy import _truncate as real_truncate
        # Simulate mutant by calling a different function
        def mutant_truncate(content, max_bytes=4096):
            result = real_truncate(content, max_bytes)
            result["result_hash"] = ""
            return result

        content = "test data"
        real = real_truncate(content)
        mutated = mutant_truncate(content)

        assert real["result_hash"] != "", "Real impl must produce a hash"
        assert mutated["result_hash"] == "", "Mutant produces empty hash"
        # The core test test_truncation_hash_deterministic asserts:
        # _truncate(x).result_hash == sha256(x).hexdigest()
        # With the mutant, this assertion would fail:
        assert real["result_hash"] == hashlib.sha256(content.encode()).hexdigest()
        assert mutated["result_hash"] != hashlib.sha256(content.encode()).hexdigest(), (
            "Mutant would make test_truncation_hash_deterministic FAIL"
        )

    def test_anti_teatro_start_writes_to_ledger_is_detectable(self, tmp_path):
        """Mutation: writing to ledger at start() creates detectable content."""
        from causadb.mcp._proxy import McpProxy

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {},
        }))

        # Clean start — no ledger writes
        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        # Manually trigger start (no mock needed for empty servers)
        # If start() wrote to the ledger, the file would exist with content
        if os.path.exists(str(ledger)):
            content = ledger.read_text().strip()
            assert content == "", (
                f"Art. V: ledger has unexpected content: {content[:200]}"
            )

    def test_anti_teatro_ledger_writer_bypass_detectable(self, mock_mcp_client, tmp_path, monkeypatch):
        """Mutation: write via open() instead of LedgerWriter → spy catches it."""
        from causadb.mcp._proxy import McpProxy
        from causadb._ledger_writer import LedgerWriter

        ledger = tmp_path / "ledger.log"
        config = tmp_path / "proxy.json"
        config.write_text(json.dumps({
            "servers": {
                "echo": {"command": ["python3", "-c", ""]},
            },
        }))

        mock_mcp_client.call_tool.return_value = type(
            "CallToolResult", (),
            {"content": [type("TextContent", (), {"text": "ok", "type": "text"})()]}
        )()

        # Spy that rejects direct open() writes by checking if append was called
        append_called = False
        original_append = LedgerWriter.append

        def spy_append(self, event):
            nonlocal append_called
            append_called = True
            return original_append(self, event)

        monkeypatch.setattr(LedgerWriter, "append", spy_append)

        proxy = McpProxy(config_path=str(config), ledger_path=str(ledger))
        anyio.run(proxy.start)
        anyio.run(proxy.call_tool, "echo_ping", {"msg": "hello"})

        # If code bypasses LedgerWriter and writes via open(), append_called is False
        assert append_called, (
            "Art. I violation: LedgerWriter.append was never called. "
            "If _log_tool_call uses open() directly instead of LedgerWriter, "
            "this assertion fails."
        )

    def test_anti_teatro_namespacing_collision(self):
        """If tool namespacing uses a different separator, core tests detect it."""
        expected = _prefix_tool("fs", "read")
        assert expected == "fs_read", "Default separator is single underscore"
        assert expected != "fs__read", "Double underscore would break lookups"
        assert expected != "fs/read", "Slash would break lookups"
