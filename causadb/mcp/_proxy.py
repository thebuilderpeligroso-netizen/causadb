"""F.9 — MCP Middleware Proxy.

Acts as a middleware proxy between an MCP client (agent) and one or more MCP
servers. Every tool call is automatically logged as TOOL_CALLED to the CausaDB
ledger via LedgerWriter.append() (Article I — Ledger Monism).

Design:
  - Uses mcp SDK (stdio_client + ClientSession) — no subprocess management
  - Tool namespacing: ``server_tool`` (single underscore)
  - Result truncation at configurable bytes (default 4096) + SHA-256 hash
  - Config file at ``~/.config/causadb/proxy.json`` (optional)
  - "Log only" fallback mode when no config exists
  - Lazy restart on crash (call_tool detects dead server and relaunches)
  - Art. V: Ledger is NEVER touched at start() time — only on call_tool()
"""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

@dataclass
class ServerState:
    name: str
    config: dict
    process: Optional[Any] = None
    session: Optional[Any] = None
    alive: bool = False
    start_time: float = 0.0
    tools: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public helpers (also exposed for unit testing)
# ---------------------------------------------------------------------------

def _prefix_tool(server_name: str, tool_name: str) -> str:
    """Namespace a tool name with its server: ``server_tool``."""
    return f"{server_name}_{tool_name}"


def _truncate(content: str, max_bytes: int = 4096) -> dict:
    """Truncate *content* to *max_bytes*, return metadata dict.

    Returns:
        dict with keys: truncated, was_truncated, result_hash, result_length
    """
    raw = content.encode("utf-8")
    content_hash = hashlib.sha256(raw).hexdigest()
    if len(raw) <= max_bytes:
        return {
            "truncated": raw.decode("utf-8", errors="replace"),
            "was_truncated": False,
            "result_hash": content_hash,
            "result_length": len(raw),
        }
    truncated = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "truncated": truncated,
        "was_truncated": True,
        "result_hash": content_hash,
        "result_length": len(raw),
    }


# ---------------------------------------------------------------------------
# Default config path
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/causadb/proxy.json")


# ---------------------------------------------------------------------------
# McpProxy — public API
# ---------------------------------------------------------------------------

class McpProxy:
    """MCP Middleware Proxy.

    Usage::

        proxy = McpProxy(config_path="/path/to/proxy.json",
                         ledger_path="/path/to/ledger.log")
        await proxy.start()
        tools = await proxy.list_tools()
        result = await proxy.call_tool("filesystem_read_file", {"path": "/tmp"})
        await proxy.stop()
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        ledger_path: Optional[str] = None,
    ):
        self._config_path = config_path or DEFAULT_CONFIG_PATH
        self._ledger_path = ledger_path
        self._writer = LedgerWriter(ledger_path) if ledger_path else None
        self._servers: dict[str, ServerState] = {}
        self._tool_map: dict[str, str] = {}  # prefixed_name → server_name
        self._config: dict = {}
        self._truncation_bytes = 4096

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> list[str]:
        """Read config and connect to all MCP servers.

        Best-effort: if one server fails to start, others continue.
        Returns list of successfully started server names.
        """
        self._load_config()
        started = []
        for name, cfg in self._config.get("servers", {}).items():
            try:
                state = await self._connect_server(name, cfg)
                self._servers[name] = state
                started.append(name)
            except Exception as e:
                # Best-effort: log and continue
                pass
        # Build reverse tool map
        self._rebuild_tool_map()
        return started

    async def stop(self) -> None:
        """Gracefully shut down all servers."""
        for name, state in list(self._servers.items()):
            try:
                if state.session is not None:
                    await state.session.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                if state.process is not None:
                    state.process.terminate()
            except Exception:
                pass
            state.alive = False
            state.session = None
            state.process = None

    # ------------------------------------------------------------------
    # Tool operations
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict]:
        """Aggregate tools from all servers with prefixed names."""
        all_tools = []
        for name, state in self._servers.items():
            if not state.alive or state.session is None:
                continue
            try:
                result = await state.session.list_tools()
                for tool in getattr(result, "tools", []):
                    all_tools.append({
                        "name": _prefix_tool(name, tool.name),
                        "server": name,
                        "original_name": tool.name,
                        "inputSchema": getattr(tool, "inputSchema", {}),
                    })
            except Exception:
                pass
        return all_tools

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Call a tool by its prefixed name, auto-log TOOL_CALLED to ledger.

        Raises:
            ValueError if the tool name cannot be parsed.
            Any exception from the MCP server (propagated).
        """
        # Parse prefixed name
        if "_" not in name:
            raise ValueError(f"Invalid tool name (missing server prefix): {name}")
        server_name, tool_name = name.split("_", 1)
        if server_name not in self._servers:
            raise ValueError(f"Unknown server: {server_name}")

        state = self._servers[server_name]

        # Lazy restart if dead
        if not state.alive or state.session is None:
            new_state = await self._connect_server(server_name, state.config)
            self._servers[server_name] = new_state
            state = new_state

        start_time = time.time()
        error = None
        result_text = ""
        result_data = None

        try:
            result_data = await state.session.call_tool(tool_name, arguments)
            # Extract text content
            text_parts = []
            for block in getattr(result_data, "content", []):
                text = getattr(block, "text", "")
                if text:
                    text_parts.append(text)
            result_text = "\n".join(text_parts)
        except Exception as e:
            error = str(e)
            raise
        finally:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_tool_call(
                tool_name=name,
                arguments=arguments,
                result=result_text,
                duration_ms=duration_ms,
                error=error,
                server=server_name,
            )

        return result_data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_config(self):
        """Load config from file. If file doesn't exist, use empty config (log-only)."""
        if os.path.exists(self._config_path):
            with open(self._config_path) as f:
                self._config = json.load(f)
        else:
            self._config = {}
        self._truncation_bytes = self._config.get("truncation_bytes", 4096)

    async def _connect_server(self, name: str, cfg: dict) -> ServerState:
        """Connect to a single MCP server via stdio."""
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession

        command = cfg.get("command", [])
        if not command:
            raise ValueError(f"No command for server '{name}'")

        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
        )

        state = ServerState(name=name, config=cfg)

        try:
            streams = await stdio_client(params).__aenter__()
            read, write = streams

            session = await ClientSession(read, write).__aenter__()
            await session.initialize()

            state.session = session
            state.alive = True
            state.start_time = time.time()

            # Fetch tools list
            tools_result = await session.list_tools()
            state.tools = getattr(tools_result, "tools", [])
        except Exception:
            # Cleanup on failure
            try:
                if state.session is not None:
                    await state.session.__aexit__(None, None, None)
            except Exception:
                pass
            raise

        return state

    def _rebuild_tool_map(self):
        """Rebuild the reverse mapping from prefixed name → server name."""
        self._tool_map = {}
        for name, state in self._servers.items():
            for tool in state.tools:
                prefixed = _prefix_tool(name, getattr(tool, "name", ""))
                self._tool_map[prefixed] = name

    def _log_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        result: str,
        duration_ms: int,
        error: Optional[str] = None,
        server: Optional[str] = None,
    ):
        """Log a TOOL_CALLED event to the ledger (Article I).

        Art. V: this is the ONLY point where the ledger is touched.
        """
        if self._writer is None:
            return

        trunc = _truncate(result, self._truncation_bytes)

        payload = {
            "tool_name": tool_name,
            "arguments": arguments,
            "result": trunc["truncated"] if not error else None,
            "result_hash": trunc["result_hash"],
            "truncated": trunc["was_truncated"],
            "result_length": trunc["result_length"],
            "duration_ms": duration_ms,
            "latency_ms": duration_ms,
            "server": server,
            "error": error,
        }

        event = CanonicalEvent(
            event_type=EventType.TOOL_CALLED,
            ctx_id="mcp-proxy",
            source="causadb:mcp-proxy",
            source_type="agent",
            payload=payload,
        )
        self._writer.append(event)
