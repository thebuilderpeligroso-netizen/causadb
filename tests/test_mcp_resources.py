"""Tests for MCP resources (F.1 — causadb://events, causadb://state, causadb://config).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They verify that FastMCP resources are properly registered
and return valid data.

Async pattern: Plan B — `anyio.run(...)` inside sync test functions.
"""
import json

import anyio
import pytest

from causadb.mcp.server import create_server
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


def _list_resource_uris(server):
    """Return a set of URI strings from the server's list_resources()."""
    async def _run():
        resources = await server.list_resources()
        return {str(r.uri) for r in resources}
    return anyio.run(_run)


def _list_resource_template_uris(server):
    """Return a set of URI template strings from list_resource_templates()."""
    async def _run():
        templates = await server.list_resource_templates()
        return {t.uriTemplate for t in templates}
    return anyio.run(_run)


def _read_resource(server, uri: str):
    """Read a resource URI and return its content text."""
    async def _run():
        contents = await server.read_resource(uri)
        return contents[0].content
    return anyio.run(_run)


def _all_uris(server):
    """Return all resource URIs (static + template)."""
    return _list_resource_uris(server) | _list_resource_template_uris(server)


# ---------------------------------------------------------------------------
# 1. causadb://events resource
# ---------------------------------------------------------------------------

def test_mcp_events_resource_registered():
    """list_resources or list_resource_templates must include `causadb://events`.

    Anti-teatro: a stub server without the @mcp.resource() for events would
    fail because the URI would be absent from the resource listing.
    """
    server = create_server()
    uris = _all_uris(server)
    assert "causadb://events" in uris, (
        f"causadb://events not found in resource URIs: {uris}"
    )


def test_mcp_events_resource_returns_json(tmp_path):
    """causadb://events reads returns a JSON array of ledger events.

    After writing 2 events to the ledger, reading the resource must return
    a JSON array with exactly 2 entries.

    Anti-teatro: a stub that returns "[]" without reading the ledger would
    fail because we assert len == 2.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/a", "action": "create"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx2", source="test2",
        payload={"path": "/b", "action": "modify"},
    ))

    server = create_server(config_ledger_path=ledger_path)
    text = _read_resource(server, "causadb://events")
    data = json.loads(text)
    assert isinstance(data, list), f"expected list, got {type(data).__name__}"
    assert len(data) == 2, f"expected 2 events, got {len(data)}"
    # Both entries must have event.event_type
    for entry in data:
        assert "event" in entry, f"missing 'event' key in {entry}"
        assert "event_type" in entry["event"], (
            f"missing event_type in {entry['event']}"
        )


# ---------------------------------------------------------------------------
# 2. causadb://state resource
# ---------------------------------------------------------------------------

def test_mcp_state_resource_registered():
    """list_resources must include `causadb://state`.

    Anti-teatro: a stub server without the @mcp.resource() for state would
    fail because the URI would be absent.
    """
    server = create_server()
    uris = _list_resource_uris(server)
    assert "causadb://state" in uris, (
        f"causadb://state not found in resource URIs: {uris}"
    )


def test_mcp_state_resource_returns_state(tmp_path):
    """causadb://state reads returns reconstructed state with events_applied.

    After writing 1 event to the ledger, reading the state resource must
    return JSON with events_applied == 1 and files_modified containing the
    written file.

    Anti-teatro: a stub returning "{}" would fail because we assert
    events_applied >= 1 and files_modified[0]["path"] == "/foo".
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server(config_ledger_path=ledger_path)
    text = _read_resource(server, "causadb://state")
    state = json.loads(text)

    assert "events_applied" in state, f"missing events_applied in {state}"
    assert "files_modified" in state, f"missing files_modified in {state}"
    assert state["events_applied"] >= 1, (
        f"expected events_applied >= 1, got {state['events_applied']}"
    )
    assert len(state["files_modified"]) == 1, (
        f"expected 1 file_modified, got {len(state['files_modified'])}"
    )
    assert state["files_modified"][0]["path"] == "/foo"


# ---------------------------------------------------------------------------
# 3. Anti-teatro: both resources must be present
# ---------------------------------------------------------------------------

def test_anti_teatro_mcp_resources_missing(tmp_path):
    """Both causadb://events and causadb://state must be registered.

    Anti-teatro: if someone removes either @mcp.resource() decorator from
    server.py, this test will fail because the corresponding URI will be
    absent from the resource listing.

    This test is redundant with the individual registration tests above,
    but it explicitly captures the invariant that both resources MUST be
    present together — a server with state but no events (or vice versa)
    is incomplete.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/demo", "action": "create"},
    ))

    server = create_server(config_ledger_path=ledger_path)
    uris = _all_uris(server)

    # Both must be registered
    assert "causadb://events" in uris, (
        "causadb://events resource is missing"
    )
    assert "causadb://state" in uris, (
        "causadb://state resource is missing"
    )

    # Both must return parseable JSON with real data
    events_text = _read_resource(server, "causadb://events")
    events = json.loads(events_text)
    assert isinstance(events, list), (
        f"events must be a JSON list, got {type(events).__name__}"
    )
    assert len(events) >= 1, (
        f"events must contain at least 1 entry (wrote 1), got {len(events)}"
    )

    state_text = _read_resource(server, "causadb://state")
    state = json.loads(state_text)
    assert isinstance(state, dict), (
        f"state must be a JSON dict, got {type(state).__name__}"
    )
    assert state["events_applied"] >= 1, (
        f"state must have events_applied >= 1, got {state['events_applied']}"
    )


# ---------------------------------------------------------------------------
# 4. causadb://config resource
# ---------------------------------------------------------------------------

def test_config_resource_registered():
    """list_resources must include `causadb://config`.

    Anti-teatro: a stub server without the @mcp.resource() for config would
    fail because the URI would be absent from the resource listing.
    """
    server = create_server()
    uris = _list_resource_uris(server)
    assert "causadb://config" in uris, (
        f"causadb://config not found in resource URIs: {uris}"
    )


def test_config_resource_returns_real_data(tmp_path):
    """causadb://config returns real config with ledger_path, workspace_path, version.

    After creating a server with a real ledger, reading the config resource
    must return JSON with the expected keys and non-stub values.

    Anti-teatro: a stub returning a hardcoded string would fail because we
    assert the ledger_path matches the one we passed and version is a non-empty
    string.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/demo", "action": "create"},
    ))

    server = create_server(config_ledger_path=ledger_path)
    text = _read_resource(server, "causadb://config")
    config = json.loads(text)

    assert "ledger_path" in config, f"missing ledger_path in {config}"
    assert "workspace_path" in config, f"missing workspace_path in {config}"
    assert "version" in config, f"missing version in {config}"

    # ledger_path must match the one we passed (real, not a stub)
    assert config["ledger_path"] == ledger_path, (
        f"expected ledger_path={ledger_path}, got {config['ledger_path']}"
    )

    # version must be a non-empty string (real version, not a stub)
    assert isinstance(config["version"], str), (
        f"version must be a string, got {type(config['version']).__name__}"
    )
    assert len(config["version"]) > 0, "version must not be empty"
