"""Tests for the streamable-http (network) security proof of CausaDB MCP.

Proof of interoperability: expose the MCP server over HTTP (streamable-http)
in a SAFE way. The proof runs on LOOPBACK (127.0.0.1) — nothing is exposed
to the internet.

Security invariants tested here (all fail-closed, Art. IX):
  1. Bind-safety: a non-loopback host WITHOUT an API key must refuse to start
     (SystemExit). A loopback host is always allowed.
  2. Tool subset: in network mode only the safe read-only tools remain
     {revive, query, ocb_status, validate, sentinel}; all write/sensitive
     tools (log, replay, recover, shared_document_write, …) are removed.
  3. Explicit ledger: network mode requires an explicit ledger (--ledger or
     CAUSADB_LEDGER_PATH); without it the server refuses to start.
  4. Redaction: query/revive outputs are redacted with `redact_payload`
     (sensitive fields hashed) before being returned.

Anti-teatro (Art. IX): every test verifies REAL behavior — that the subset
really excludes write tools, that bind-safety really fails on non-loopback
without a key, that redaction really hashes a sensitive field. No trivial
assertions.

CRITICAL: these tests must NOT break the ~21 tests that use `create_server()`.
The security subset is applied in `main()` / new helper functions, NOT in
`create_server()`.
"""
import hashlib
import json
import os

import pytest
import anyio

from causadb.mcp.server import (
    create_server,
    _check_bind_safety,
    _require_explicit_ledger,
    _apply_http_tool_subset,
    _apply_http_resource_subset,
    _apply_http_security,
    _redact_json_output,
    HTTP_SAFE_TOOLS,
)
from causadb._config import CausaDBConfig
from causadb._redactor import redact_payload
from tests.helpers._mcp_call import _call_tool


def _text(content_blocks):
    """Concatenate `.text` from all TextContent blocks into a single string."""
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


# ---------------------------------------------------------------------------
# 1. Bind-safety (OpenJarvis check_bind_safety pattern)
# ---------------------------------------------------------------------------

def test_bind_safety_non_loopback_without_key_fails():
    """Non-loopback host (0.0.0.0) without an API key → SystemExit(1).

    Anti-teatro: a stub that ignores the host and always starts would fail
    because the test asserts SystemExit is raised.
    """
    with pytest.raises(SystemExit) as exc_info:
        _check_bind_safety("0.0.0.0", None)
    assert exc_info.value.code == 1


def test_bind_safety_non_loopback_with_key_ok():
    """Non-loopback host WITH an API key is allowed (operator opted in)."""
    # Should NOT raise.
    _check_bind_safety("0.0.0.0", "some-api-key")


def test_bind_safety_loopback_ok_without_key():
    """Loopback hosts are always allowed, even without an API key.

    Anti-teatro: a stub that fails on loopback would break the local proof.
    """
    for host in ("127.0.0.1", "localhost", "", "::1"):
        _check_bind_safety(host, None)  # must not raise


# ---------------------------------------------------------------------------
# 2. Tool subset in network mode
# ---------------------------------------------------------------------------

def test_http_tool_subset_returns_only_safe_tools():
    """The subset function returns exactly {revive, query, ocb_status,
    validate, sentinel} and removes every write/sensitive tool.

    Anti-teatro: a stub that keeps `log`/`replay`/`recover` would fail the
    exact-set assertion AND the post-removal tool list assertion.
    """
    server = create_server()
    remaining = anyio.run(_apply_http_tool_subset, server)

    assert remaining == HTTP_SAFE_TOOLS, (
        f"subset must be exactly {sorted(HTTP_SAFE_TOOLS)}, got {sorted(remaining)}"
    )

    # Verify the server really dropped the write/sensitive tools.
    async def _list():
        return await server.list_tools()
    names = {t.name for t in anyio.run(_list)}
    assert names == HTTP_SAFE_TOOLS, (
        f"server must expose ONLY safe tools, got {sorted(names)}"
    )
    for removed in ("log", "replay", "recover", "shared_document_write",
                    "log_decision", "chronicle_append", "ocb_load_partition",
                    "feedback", "sandbox", "stream", "impact", "why", "trace",
                    "score", "skill_list", "shared_document_read"):
        assert removed not in names, f"write/sensitive tool {removed!r} still exposed"


def test_http_tool_subset_keeps_create_server_intact():
    """Applying the subset to one server must NOT affect `create_server()`.

    Anti-teatro: if the subset mutated a module-level singleton or leaked
    state, a fresh `create_server()` would come back with fewer tools.
    """
    server = create_server()
    anyio.run(_apply_http_tool_subset, server)

    fresh = create_server()
    async def _list():
        return await fresh.list_tools()
    names = {t.name for t in anyio.run(_list)}
    assert len(names) == 21, (
        f"create_server() must still expose 21 tools, got {len(names)}"
    )


# ---------------------------------------------------------------------------
# 3. Explicit ledger in network mode
# ---------------------------------------------------------------------------

def test_http_ledger_required_without_ledger_fails(monkeypatch):
    """Network mode without --ledger nor CAUSADB_LEDGER_PATH → SystemExit(1).

    Anti-teatro: a stub that silently falls back to CWD auto-init would fail
    (the whole point is to NOT resolve the ledger from CWD in network mode).
    """
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        _require_explicit_ledger(None)
    assert exc_info.value.code == 1


def test_http_ledger_required_with_flag_ok(tmp_path):
    """Network mode with an explicit --ledger path is accepted."""
    lp = str(tmp_path / "ledger.log")
    assert _require_explicit_ledger(lp) == os.path.abspath(lp)


def test_http_ledger_required_from_env_ok(tmp_path, monkeypatch):
    """Network mode resolves the ledger from CAUSADB_LEDGER_PATH when the
    --ledger flag is absent."""
    lp = str(tmp_path / "ledger.log")
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", lp)
    assert _require_explicit_ledger(None) == os.path.abspath(lp)


# ---------------------------------------------------------------------------
# 4. Redaction (redact_payload) in network mode
# ---------------------------------------------------------------------------

def test_redact_payload_hashes_sensitive_fields():
    """`redact_payload` hashes sensitive fields (api_key/token) and leaves
    non-sensitive fields untouched.

    Anti-teatro: a stub that returns the payload unchanged would fail the
    hash assertion.
    """
    config = CausaDBConfig(ledger_path="/tmp/x.log")
    payload = {"api_key": "secret123", "token": "tok", "path": "/foo"}
    out = redact_payload(payload, config)

    assert out["api_key"] != "secret123"
    assert out["api_key"] == hashlib.sha256(b"secret123").hexdigest()[:16]
    assert out["token"] != "tok"
    assert out["token"] == hashlib.sha256(b"tok").hexdigest()[:16]
    assert out["path"] == "/foo", "non-sensitive field must be preserved"


def test_redact_json_output_redacts_nested_payload():
    """`_redact_json_output` redacts sensitive fields nested inside a
    query-like JSON envelope (events[].event.payload).

    Anti-teatro: a stub that only redacts the top level would fail because
    the sensitive field lives two levels deep.
    """
    config = CausaDBConfig(ledger_path="/tmp/x.log")
    text = json.dumps({
        "events": [{"event": {"payload": {"api_key": "s3cr3t", "path": "/f"}}}],
        "truncated": False,
    })
    out = json.loads(_redact_json_output(text, config))
    payload = out["events"][0]["event"]["payload"]
    assert payload["api_key"] != "s3cr3t"
    assert payload["api_key"] == hashlib.sha256(b"s3cr3t").hexdigest()[:16]
    assert payload["path"] == "/f"


def test_http_query_wrapper_redacts_raw_payload(monkeypatch, tmp_path):
    """Anti-teatro: after `_apply_http_security`, the `query` tool is wrapped
    so that even if the underlying tool returns a RAW sensitive payload, the
    exposed output is redacted.

    We monkeypatch `_tools.causadb_query` to return a raw secret to prove the
    wrapper (not the ledger's own write-time redaction) is doing the work.
    """
    from causadb.mcp import _tools

    ledger_path = str(tmp_path / "ledger.log")
    server = create_server(config_ledger_path=ledger_path)
    config = CausaDBConfig(ledger_path=ledger_path)
    anyio.run(_apply_http_security, server, config)

    # Force the underlying query to return a raw sensitive payload.
    monkeypatch.setattr(_tools, "causadb_query", lambda **kw: json.dumps({
        "events": [{"event": {"payload": {"api_key": "raw-secret"}}}],
        "truncated": False,
    }))

    content_blocks, _ = _call_tool(server, "query", {"ledger_path": ledger_path})
    data = json.loads(_text(content_blocks))
    payload = data["events"][0]["event"]["payload"]
    assert payload["api_key"] != "raw-secret"
    assert payload["api_key"] == hashlib.sha256(b"raw-secret").hexdigest()[:16]


# ---------------------------------------------------------------------------
# 5. Resources in network mode
# ---------------------------------------------------------------------------

def test_http_resources_exclude_events_and_state():
    """Network mode must NOT expose causadb://events nor causadb://state
    (they dump the whole ledger). Only config/canon remain.

    Anti-teatro: a stub that keeps events/state would fail the membership
    assertions.
    """
    server = create_server()
    remaining = _apply_http_resource_subset(server)

    assert "causadb://events" not in remaining
    assert "causadb://state" not in remaining
    assert "causadb://config" in remaining
    assert "causadb://canon" in remaining