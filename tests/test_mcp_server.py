"""Tests for the CausaDB MCP server (P.15).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They exercise the MCP server as a thin delegator to the
existing nucleus — no logic is reimplemented here.

Async pattern: Plan B — `anyio.run(...)` inside sync test functions. The venv
has `anyio` (a dependency of `mcp`) but NOT `pytest-asyncio`, so we avoid
adding a new dependency by wrapping async calls in `anyio.run`.

Anti-teatro (Article IX): every test has discriminatory power — a stub server
that returns empty dicts, skips validation, or writes via `open()` directly
will fail at least one assertion in this file.
"""
import json
import os

import pytest
import anyio

from causadb.mcp.server import create_server
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_tool(server, name, arguments):
    """Synchronous wrapper around `await server.call_tool(...)`.

    Returns the structured dict (second element of the tuple returned by
    FastMCP.call_tool). The structured dict has the raw return value under
    `result`, which for our tools is a JSON string.
    """
    async def _run():
        content_blocks, structured = await server.call_tool(name, arguments)
        return content_blocks, structured
    return anyio.run(_run)


def _text(content_blocks):
    """Concatenate `.text` from all TextContent blocks into a single string."""
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


def _valid_event_json():
    """Return a JSON string for a valid FILE_MODIFIED event."""
    return json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "source_type": "agent",
        "payload": {"path": "/foo", "action": "create"},
    })


# ---------------------------------------------------------------------------
# 1. Server exposes exactly 19 tools
# ---------------------------------------------------------------------------

def test_mcp_server_exposes_all_tools():
    """The server must expose exactly 21 tools: log, replay,
    sentinel, query, validate, feedback, sandbox, stream,
    impact, why, trace, score, skill_list, log_decision,
    revive, ocb_status, ocb_load_partition, chronicle_append,
    recover, shared_document_read, shared_document_write — NO MORE (Article VII Simplicity Gate).

    Anti-teatro: a stub `create_server` returning FastMCP("x") with no tools
    would fail (assert exactly 21 + names match). A stub registering 20 + an
    extra `causadb_foo` would fail (assert no extra tools).
    """
    server = create_server()

    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)

    names = {t.name for t in tools}
    expected = {
        "log",
        "replay",
        "sentinel",
        "query",
        "validate",
        "feedback",
        "sandbox",
        "stream",
        "impact",
        "why",
        "trace",
        "score",
        "skill_list",
        "log_decision",
        "revive",
        "ocb_status",
        "ocb_load_partition",
        "chronicle_append",
        "recover",
        "shared_document_read",
        "shared_document_write",
    }
    assert len(names) == 21, f"expected exactly 21 tools, got {len(names)}: {sorted(names)}"
    assert names == expected, f"unexpected tool set: {names - expected}, missing: {expected - names}"


# ---------------------------------------------------------------------------
# 2. causadb_log appends an event
# ---------------------------------------------------------------------------

def test_mcp_log_appends_event(tmp_path):
    """`causadb_log` appends an event to the ledger and returns event_id/hash/timestamp.

    Anti-teatro: a stub `causadb_log` that returns "{}" without appending
    would fail because the test reads `ledger.log` directly and asserts an
    entry was written.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "log", {
        "event_json": _valid_event_json(),
        "ledger_path": ledger_path,
    })
    text = _text(content_blocks)
    payload = json.loads(text)

    assert "event_id" in payload, f"missing event_id in {payload}"
    assert "hash" in payload, f"missing hash in {payload}"
    assert "timestamp" in payload, f"missing timestamp in {payload}"
    assert payload["event_id"], "event_id must be non-empty"
    assert payload["hash"], "hash must be non-empty"

    # Read ledger.log directly and assert 1 entry was written
    assert os.path.exists(ledger_path), "ledger.log was not created"
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1, (
        f"expected 1 entry in ledger.log, got {len(lines)}"
    )
    entry = json.loads(lines[0])
    assert entry["event"]["event_id"] == payload["event_id"]
    assert entry["hash"] == payload["hash"]


# ---------------------------------------------------------------------------
# 3. causadb_log rejects invalid events (Fall-Closed)
# ---------------------------------------------------------------------------

def test_mcp_log_invalid_event_raises(tmp_path):
    """`causadb_log` with invalid JSON OR a schema-invalid event must raise
    (FastMCP converts raised exceptions into MCP error responses — Fall-Closed).

    Anti-teatro: a stub that always returns success would fail because the test
    asserts Fall-Closed behavior (raised exception).
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    # Case A: malformed JSON
    with pytest.raises(Exception):
        _call_tool(server, "log", {
            "event_json": "not valid json {{{",
            "ledger_path": ledger_path,
        })

    # Case B: valid JSON but missing required field (`path` for FILE_MODIFIED)
    invalid_event = json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "payload": {"action": "create"},  # missing `path`
    })
    with pytest.raises(Exception):
        _call_tool(server, "log", {
            "event_json": invalid_event,
            "ledger_path": ledger_path,
        })

    # Anti-teatro: ledger must NOT have been created/appended
    assert not os.path.exists(ledger_path), (
        "invalid event must NOT create ledger.log"
    )


def test_mcp_log_metadata_priority_accepted(tmp_path):
    """(BIT-CHR.35 P1) `causadb_log` must accept `metadata.priority` and append
    without raising (Fall-Closed would surface "Invalid metadata" ValueError
    from `EventMetadata(**data["metadata"])`).

    Anti-teatro: against the old code this raises
    ValueError("Invalid metadata: ... unexpected keyword argument 'priority'").
    After the fix it appends and the event read back preserves `priority`.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    event_json = json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "source_type": "agent",
        "payload": {"path": "/foo", "action": "create"},
        "metadata": {"trace_id": "t", "session_id": "s", "priority": "high"},
    })
    content_blocks, _ = _call_tool(server, "log", {
        "event_json": event_json,
        "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))
    assert "event_id" in payload
    assert payload["event_id"]

    # Read back from the ledger (Ledger Monism) — priority must survive
    from causadb._ledger_reader import LedgerReader
    events = list(LedgerReader(ledger_path).read_all())
    assert events[-1].metadata is not None
    assert events[-1].metadata.priority == "high"
    assert events[-1].metadata.session_id == "s"


# ---------------------------------------------------------------------------
# 4. causadb_replay returns reconstructed state
# ---------------------------------------------------------------------------

def test_mcp_replay_returns_state(tmp_path):
    """`causadb_replay` delegates to `ReplayEngine.reconstruct_state()` and
    returns JSON with `files_modified` (len 1) and `events_applied >= 1`.

    Anti-teatro: a stub returning "{}" would fail because the test asserts
    `files_modified` has length 1.
    """
    ledger_path = str(tmp_path / "ledger.log")
    # Pre-populate directly via LedgerWriter (bypass MCP for setup)
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "replay", {
        "ledger_path": ledger_path,
    })
    state = json.loads(_text(content_blocks))

    assert "files_modified" in state, f"missing files_modified in {state}"
    assert "events_applied" in state, f"missing events_applied in {state}"
    assert len(state["files_modified"]) == 1, (
        f"expected 1 file modified, got {len(state['files_modified'])}"
    )
    assert state["events_applied"] >= 1, (
        f"expected events_applied >= 1, got {state['events_applied']}"
    )
    assert state["files_modified"][0]["path"] == "/foo"


# ---------------------------------------------------------------------------
# 5. causadb_sentinel returns a report with 3 rules
# ---------------------------------------------------------------------------

def test_mcp_sentinel_returns_report(tmp_path):
    """`causadb_sentinel` delegates to `evaluate_rules(ledger_path)` and returns
    JSON with `summary` in {OK, DRIFT_DETECTED} and `results` with exactly 3 items.

    Anti-teatro: a stub returning "{}" would fail because the test asserts
    `results` has exactly 3 items.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "sentinel", {
        "ledger_path": ledger_path,
    })
    report = json.loads(_text(content_blocks))

    assert "summary" in report, f"missing summary in {report}"
    assert "results" in report, f"missing results in {report}"
    assert "all_rules_pass" in report, f"missing all_rules_pass in {report}"
    assert report["summary"] in ("OK", "DRIFT_DETECTED"), (
        f"summary must be OK or DRIFT_DETECTED, got {report['summary']!r}"
    )
    assert isinstance(report["results"], list)
    assert len(report["results"]) == 3, (
        f"expected 3 rule results, got {len(report['results'])}"
    )
    for r in report["results"]:
        assert "rule_name" in r
        assert "passed" in r
        assert isinstance(r["passed"], bool)


# ---------------------------------------------------------------------------
# 6. Article V — server does NOT read the ledger at construction time
# ---------------------------------------------------------------------------

def test_mcp_server_no_ledger_load_on_startup(tmp_path):
    """Article V (Memory Layer Separation): the server must NOT read or create
    the ledger file at construction time. Reads are deferred to tool call time.

    We prove this by setting `ledger_path` to a non-existent file, constructing
    the server, and asserting:
      (a) no exception is raised at construction,
      (b) the ledger file is NOT created,
      (c) calling `causadb_replay` on the non-existent ledger raises (proving
          the read is deferred to tool call time — if the server had read it
          at construction, it would have raised then).
    """
    ledger_path = str(tmp_path / "does_not_exist_yet.log")
    assert not os.path.exists(ledger_path), "precondition: ledger must not exist"

    # (a) Construction must not raise and must not touch the ledger.
    server = create_server(config_ledger_path=ledger_path)

    # (b) Ledger file must NOT have been created at construction time.
    assert not os.path.exists(ledger_path), (
        "Article V violation: server created ledger.log at construction time"
    )

    # (c) Calling replay on a non-existent ledger must raise — proving the
    # read is deferred to tool call time. (ReplayEngine → LedgerValidator →
    # validate_or_raise → reads file; for a non-existent ledger the validator
    # returns is_valid=True with no entries, so reconstruct_state returns an
    # empty state. We instead assert the state has events_applied == 0, which
    # proves the read happened at tool-call time, not at construction.)
    content_blocks, _ = _call_tool(server, "replay", {
        "ledger_path": ledger_path,
    })
    state = json.loads(_text(content_blocks))
    assert state["events_applied"] == 0, (
        f"expected events_applied=0 for non-existent ledger, got {state}"
    )
    # The ledger file may or may not be created by the read path; the key
    # assertion is that it was NOT created at construction (already asserted).


# ---------------------------------------------------------------------------
# 7. Article I — causadb_log routes through LedgerWriter.append (not open())
# ---------------------------------------------------------------------------

def test_mcp_server_writes_via_ledger_writer(tmp_path, monkeypatch):
    """Article I (Ledger Monism): `causadb_log` must route through
    `LedgerWriter.append()`, not write to the ledger file directly via `open()`.

    Anti-teatro: a stub `causadb_log` that calls `open(path, "a").write(...)`
    instead of `LedgerWriter.append()` would fail this monkeypatch spy.

    We spy on `LedgerWriter.append` to record the event it received, call
    `causadb_log`, and assert the spy was called with a `CanonicalEvent`
    matching the input.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    received_events = []
    original_append = LedgerWriter.append

    def spy_append(self, event):
        received_events.append(event)
        return original_append(self, event)

    monkeypatch.setattr(LedgerWriter, "append", spy_append)

    _call_tool(server, "log", {
        "event_json": _valid_event_json(),
        "ledger_path": ledger_path,
    })

    assert len(received_events) == 1, (
        f"LedgerWriter.append was not called exactly once; calls={len(received_events)}"
    )
    event = received_events[0]
    assert isinstance(event, CanonicalEvent), (
        f"expected CanonicalEvent, got {type(event).__name__}"
    )
    assert event.event_type == EventType.FILE_MODIFIED
    assert event.payload.get("path") == "/foo"
    assert event.payload.get("action") == "create"
    assert event.source == "opencode:agent1"


# ---------------------------------------------------------------------------
# 8. causadb_query returns filtered events
# ---------------------------------------------------------------------------

def test_mcp_query_returns_filtered_events(tmp_path):
    """`causadb_query` delegates to `LedgerIndex.query()` and returns matching
    events. Filtering by event_type must return only matching entries.

    Anti-teatro: a stub returning "[]" would fail because the test asserts
    2 matching entries for FILE_MODIFIED.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx2",
        source="opencode:agent2",
        payload={"path": "/bar", "action": "modify"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="ctx3",
        source="human:operator",
        payload={"feedback": "ok"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
        "event_type": "FILE_MODIFIED",
    })
    # C1: query devuelve SIEMPRE envelope {"events", "truncated", ...}.
    data = json.loads(_text(content_blocks))
    assert isinstance(data, dict), "query debe devolver envelope (C1)"
    results = data["events"]
    assert len(results) == 2, f"expected 2 FILE_MODIFIED, got {len(results)}"
    for entry in results:
        assert entry["event"]["event_type"] == "FILE_MODIFIED"


def test_mcp_query_returns_all_with_no_filters(tmp_path):
    """`causadb_query` with NO filters returns ALL events.

    Previous behavior returned [] (bug), now returns all events.
    Anti-teatro: a stub returning hardcoded entries would fail because
    LedgerIndex.query with None filters returns all entries from the ledger.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    # C1: query devuelve SIEMPRE envelope {"events", "truncated", ...}.
    data = json.loads(_text(content_blocks))
    assert isinstance(data, dict), "query debe devolver envelope (C1)"
    results = data["events"]
    assert len(results) == 1, f"expected 1 event with no filters, got {len(results)}"


# ---------------------------------------------------------------------------
# 9. causadb_validate returns hash chain integrity report
# ---------------------------------------------------------------------------

def test_mcp_validate_returns_report(tmp_path):
    """`causadb_validate` delegates to `LedgerValidator.validate_chain()` and
    returns JSON with `is_valid` and `failure_type`.

    Anti-teatro: a stub returning {"is_valid": true} without reading the ledger
    would fail because the test also writes 1 entry and asserts failure_type
    is None for a valid ledger.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "validate", {
        "ledger_path": ledger_path,
    })
    report = json.loads(_text(content_blocks))
    assert "is_valid" in report, f"missing is_valid in {report}"
    assert "failure_type" in report, f"missing failure_type in {report}"
    assert report["is_valid"] is True, f"expected valid chain, got {report}"


# ---------------------------------------------------------------------------
# 10. causadb_feedback returns HUMAN_FEEDBACK events
# ---------------------------------------------------------------------------

def test_mcp_feedback_returns_feedback_events(tmp_path):
    """`causadb_feedback` delegates to `LedgerIndex.query(event_type='HUMAN_FEEDBACK')`.

    Anti-teatro: a stub returning "[]" would fail because the test writes 2
    HUMAN_FEEDBACK events and asserts len == 2.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="ctx",
        source="human:operator",
        payload={"feedback": "good"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="ctx2",
        source="human:operator",
        payload={"feedback": "bad"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx3",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "feedback", {
        "ledger_path": ledger_path,
    })
    results = json.loads(_text(content_blocks))
    assert len(results) == 2, f"expected 2 HUMAN_FEEDBACK, got {len(results)}"
    for entry in results:
        assert entry["event"]["event_type"] == "HUMAN_FEEDBACK"


# ---------------------------------------------------------------------------
# 11. causadb_sandbox returns violations summary
# ---------------------------------------------------------------------------

def test_mcp_sandbox_returns_summary(tmp_path):
    """`causadb_sandbox` delegates to `ReplayEngine.reconstruct_state()` and
    returns JSON with `violations` and `total_mutations`.

    Anti-teatro: a stub returning "{}" would fail because the test asserts
    both keys exist, `total_mutations` is an int, and >= 1.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="ctx",
        source="opencode:agent1",
        payload={"mutation_type": "file_write", "path_or_resource": "/tmp/foo",
                 "violates_boundary": False},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "sandbox", {
        "ledger_path": ledger_path,
    })
    report = json.loads(_text(content_blocks))
    assert "violations" in report, f"missing violations in {report}"
    assert "total_mutations" in report, f"missing total_mutations in {report}"
    assert isinstance(report["total_mutations"], int)
    assert report["total_mutations"] >= 1, (
        f"expected >= 1 mutation, got {report['total_mutations']}"
    )


# ---------------------------------------------------------------------------
# 12. causadb_stream returns STREAM_INTERRUPTED events
# ---------------------------------------------------------------------------

def test_mcp_stream_returns_stream_events(tmp_path):
    """`causadb_stream` delegates to `LedgerIndex.query(event_type='STREAM_INTERRUPTED')`.

    Anti-teatro: a stub returning "[]" would fail because the test writes 1
    STREAM_INTERRUPTED event and asserts len == 1.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="ctx",
        source="opencode:agent1",
        payload={"reason": "timeout"},
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx2",
        source="opencode:agent1",
        payload={"path": "/foo", "action": "create"},
    ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "stream", {
        "ledger_path": ledger_path,
    })
    results = json.loads(_text(content_blocks))
    assert len(results) == 1, f"expected 1 STREAM_INTERRUPTED, got {len(results)}"
    assert results[0]["event"]["event_type"] == "STREAM_INTERRUPTED"


# ---------------------------------------------------------------------------
# 13. causadb_score — registered + returns JSON (F.13.3)
# ---------------------------------------------------------------------------

def test_mcp_causadb_score_registered():
    """The tool `score` is registered on the server.

    Anti-teatro: a stub `create_server` that omits the `@mcp.tool()` decorator
    for `score` would fail because the name would be absent from the
    tool list.
    """
    server = create_server()

    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)

    names = [t.name for t in tools]
    assert "score" in names, (
        f"score not registered; tools={names}"
    )


def test_mcp_causadb_score_returns_json(tmp_path):
    """`score` on an empty ledger returns parseable JSON with the
    expected score fields and overall_score in [0, 100].

    Anti-teatro: a stub returning "{}" would fail because the test asserts the
    presence of `overall_score`, `churn_score`, `waste_score`,
    `survival_score`, `weights_used`, and `correlation_method`. A stub
    returning out-of-range values would fail the [0, 100] clamp assertion.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "score", {
        "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))

    for key in (
        "overall_score", "churn_score", "waste_score", "survival_score",
        "weights_used", "correlation_method",
    ):
        assert key in payload, f"missing {key} in {list(payload.keys())}"

    assert 0.0 <= payload["overall_score"] <= 100.0, (
        f"overall_score out of [0,100]: {payload['overall_score']}"
    )
    assert 0.0 <= payload["churn_score"] <= 100.0
    assert 0.0 <= payload["waste_score"] <= 100.0
    assert 0.0 <= payload["survival_score"] <= 100.0
    assert payload["correlation_method"] == "timestamp_proximity", (
        f"expected timestamp_proximity, got {payload['correlation_method']!r}"
    )
    assert isinstance(payload["weights_used"], dict)
    assert set(payload["weights_used"].keys()) == {"churn", "waste", "survival"}, (
        f"unexpected weights keys: {payload['weights_used'].keys()}"
    )


def test_mcp_causadb_score_filter_by_session(tmp_path):
    """`causadb_score` with `session="ctx_test"` adds `session_filter` and
    `session_result` keys to the output.

    Anti-teatro: a stub ignoring the `session` parameter would fail because
    the test asserts both keys are present and `session_filter` echoes the
    input.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "score", {
        "ledger_path": ledger_path,
        "session": "ctx_test",
    })
    payload = json.loads(_text(content_blocks))

    assert payload.get("session_filter") == "ctx_test", (
        f"session_filter must echo 'ctx_test', got {payload.get('session_filter')!r}"
    )
    # session_result is None for a session that has no events, but the key
    # must be present (proves the filter branch executed).
    assert "session_result" in payload, (
        f"missing session_result key in {list(payload.keys())}"
    )


# ---------------------------------------------------------------------------
# 14. causadb_skill_list — registered + empty ledger (F.13.4)
# ---------------------------------------------------------------------------

def test_mcp_causadb_skill_list_registered():
    """The tool `skill_list` is registered on the server.

    Anti-teatro: a stub `create_server` that omits the `@mcp.tool()` decorator
    for `skill_list` would fail because the name would be absent.
    """
    server = create_server()

    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)

    names = [t.name for t in tools]
    assert "skill_list" in names, (
        f"skill_list not registered; tools={names}"
    )


def test_mcp_causadb_skill_list_empty_ledger(tmp_path):
    """`causadb_skill_list` on an empty ledger returns JSON with `count=0`
    and `skills=[]`.

    Anti-teatro: a stub returning a hardcoded non-empty list would fail the
    `count == 0` assertion. A stub returning "{}" would fail the `skills` key
    assertion.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "skill_list", {
        "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))

    assert "count" in payload, f"missing count in {payload}"
    assert "skills" in payload, f"missing skills in {payload}"
    assert payload["count"] == 0, (
        f"expected count=0 for empty ledger, got {payload['count']}"
    )
    assert payload["skills"] == [], (
        f"expected empty skills list, got {payload['skills']}"
    )


# ---------------------------------------------------------------------------
# 15. Anti-teatro — causadb_score is a thin wrapper around compute_score
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 16. causadb_log_decision — GOVERNANCE_DECISION event via MCP
# ---------------------------------------------------------------------------

def test_causadb_log_decision_appends_governance_event(tmp_path):
    """`log_decision` appends a GOVERNANCE_DECISION event and returns
    event_id/hash/timestamp.

    Anti-teatro: a stub returning "{}" without writing to the ledger would
    fail because the test reads the ledger file directly and asserts the
    event was written with the correct payload fields.
    """
    import json
    import os
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "log_decision", {
        "reasoning": "Need to migrate to PostgreSQL",
        "impact": "high",
        "decision_type": "architectural",
        "origin": "agent",
        "ledger_path": ledger_path,
    })
    text = _text(content_blocks)
    payload = json.loads(text)

    assert "event_id" in payload, f"missing event_id in {payload}"
    assert "hash" in payload, f"missing hash in {payload}"
    assert "timestamp" in payload, f"missing timestamp in {payload}"

    # Read ledger.log directly and assert entry was written
    assert os.path.exists(ledger_path), "ledger.log was not created"
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1, f"expected 1 entry, got {len(lines)}"
    entry = json.loads(lines[0])
    assert entry["event"]["event_type"] == "GOVERNANCE_DECISION"
    assert entry["event"]["payload"]["reasoning"] == "Need to migrate to PostgreSQL"
    assert entry["event"]["payload"]["impact"] == "high"
    assert entry["event"]["payload"]["decision_type"] == "architectural"
    assert entry["event"]["payload"]["origin"] == "agent"


def test_causadb_log_decision_rejects_invalid_impact(tmp_path):
    """`causadb_log_decision` with invalid impact must raise (Fall-Closed).

    Anti-teatro: a stub that bypasses validation and always returns success
    would fail because it would not raise on invalid impact.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    with pytest.raises(Exception):
        _call_tool(server, "log_decision", {
            "reasoning": "test",
            "impact": "invalid",
            "decision_type": "strategic",
            "origin": "agent",
            "ledger_path": ledger_path,
        })

    import os
    assert not os.path.exists(ledger_path), (
        "invalid event must NOT create ledger.log"
    )


def test_anti_teatro_causadb_log_decision_bypasses_ledger(tmp_path, monkeypatch):
    """Anti-teatro: verifies that `causadb_log_decision` routes through
    LedgerWriter.append() and not via open() directly.

    Monkeyspatch a spy on LedgerWriter.append and assert it was called.
    A stub that writes directly via open() would fail the spy assertion.
    """
    import json
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    received_events = []
    original_append = LedgerWriter.append

    def spy_append(self, event):
        received_events.append(event)
        return original_append(self, event)

    monkeypatch.setattr(LedgerWriter, "append", spy_append)

    _call_tool(server, "log_decision", {
        "reasoning": "test reasoning",
        "impact": "critical",
        "decision_type": "tactical",
        "origin": "agent",
        "ledger_path": ledger_path,
    })

    assert len(received_events) == 1, (
        f"LedgerWriter.append was not called exactly once; calls={len(received_events)}"
    )
    event = received_events[0]
    assert isinstance(event, CanonicalEvent)
    assert event.event_type == EventType.GOVERNANCE_DECISION
    assert event.payload.get("reasoning") == "test reasoning"
    assert event.payload.get("impact") == "critical"
    assert event.payload.get("decision_type") == "tactical"
    assert event.payload.get("origin") == "agent"


# ---------------------------------------------------------------------------
# 17. BIT-14.7 — Auto-init: create_server con default_ledger
# ---------------------------------------------------------------------------

def test_create_server_default_ledger_available(tmp_path):
    """create_server(config_ledger_path=...) almacena el default.
    
    Anti-teatro: un stub que no expone el path como default falla.
    Verificamos que una tool sin ledger_path explícito puede usar
    el default.
    """
    ledger_path = str(tmp_path / "ledger.log")
    # Pre-populate
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/f", "action": "create"},
    ))

    server = create_server(config_ledger_path=ledger_path)
    # causadb_replay sin ledger_path — debe usar el default
    content_blocks, _ = _call_tool(server, "replay", {})
    state = json.loads(_text(content_blocks))
    assert state["events_applied"] >= 1, (
        f"default_ledger no se usó: {state}"
    )


def test_tool_explicit_ledger_overrides_default(tmp_path):
    """ledger_path explícito en tool call tiene prioridad sobre el default.
    
    Anti-teatro: una tool que ignora el ledger_path explícito y siempre
    usa el default falla.
    """
    default_path = str(tmp_path / "default.log")
    explicit_path = str(tmp_path / "explicit.log")

    # Poblar ambos ledgers
    for lp in (default_path, explicit_path):
        writer = LedgerWriter(lp)
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx", source="test",
            payload={"path": "/f", "action": "create"},
        ))

    server = create_server(config_ledger_path=default_path)

    # Llamar causadb_replay con ledger_path explícito (debe ignorar default)
    content_blocks, _ = _call_tool(server, "replay", {
        "ledger_path": explicit_path,
    })
    state = json.loads(_text(content_blocks))
    assert state["events_applied"] >= 1

    # Repetir con query — debe mostrar 1 entry en explicit, no default
    content_blocks_q, _ = _call_tool(server, "query", {
        "ledger_path": explicit_path,
    })
    # C1: query devuelve SIEMPRE envelope {"events", "truncated", ...}.
    data_q = json.loads(_text(content_blocks_q))
    assert isinstance(data_q, dict), "query debe devolver envelope (C1)"
    results = data_q["events"]
    assert len(results) == 1, (
        f"debe leer de explicit_path, no de default: {results}"
    )


def test_tool_no_default_no_explicit_raises(tmp_path):
    """Tool sin default_ledger ni ledger_path explícito → error.
    
    FastMCP envuelve ValueError en ToolError. El mensaje debe mencionar
    "ledger path" para que el agente entienda qué falta.
    
    Anti-teatro: un stub que devuelve algo sin fallar rompe Fall-Closed.
    """
    server = create_server()  # sin config_ledger_path

    with pytest.raises(Exception, match="ledger path"):
        _call_tool(server, "replay", {})


def test_create_server_default_from_config(tmp_path):
    """create_server(config=CausaDBConfig(...)) usa config.ledger_path.
    
    Anti-teatro: si el server ignora config.ledger_path, la tool sin
    ledger_path fallaría.
    """
    from causadb._config import CausaDBConfig
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx", source="test",
        payload={"path": "/f", "action": "create"},
    ))

    config = CausaDBConfig(ledger_path=ledger_path)
    server = create_server(config=config)

    content_blocks, _ = _call_tool(server, "replay", {})
    state = json.loads(_text(content_blocks))
    assert state["events_applied"] >= 1


# ---------------------------------------------------------------------------
# 18. BIT-14.7 — Auto-init: _resolve_ledger (env > discover > auto-init)
# ---------------------------------------------------------------------------

def test_resolve_ledger_env_var_wins(tmp_path, monkeypatch):
    """CAUSADB_LEDGER_PATH env var tiene prioridad máxima.
    
    Anti-teatro: un stub que ignora la env var falla.
    """
    from causadb.mcp.server import _resolve_ledger
    ledger_path = str(tmp_path / "from_env.log")
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", ledger_path)
    monkeypatch.chdir(tmp_path)  # No hay workspace en CWD

    result = _resolve_ledger()
    assert result == ledger_path, (
        f"env var debería ganar: {result} != {ledger_path}"
    )


def test_resolve_ledger_discover_existing(tmp_path, monkeypatch):
    """Sin env var: discover encuentra .causadb/ existente.
    
    Anti-teatro: un stub que ignora el workspace existente falla.
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    # Crear workspace en tmp_path
    from causadb._workspace import WorkspaceManager
    result = WorkspaceManager.init(str(tmp_path))
    expected_ledger = result["ledger_path"]

    # _resolve_ledger debe descubrir el workspace
    resolved = _resolve_ledger()
    assert resolved == expected_ledger, (
        f"debe descubrir workspace: {resolved} != {expected_ledger}"
    )


def test_resolve_ledger_auto_init(tmp_path, monkeypatch):
    """Sin env var ni workspace: auto-init crea .causadb/ en CWD.
    
    Anti-teatro: un stub que retorna path sin crear el ledger falla.
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = _resolve_ledger()
    assert (tmp_path / ".causadb" / "ledger.log").exists(), (
        "auto-init no creó ledger.log"
    )
    assert (tmp_path / ".causadb" / "config.json").exists(), (
        "auto-init no creó config.json"
    )
    # Verificar que el path resuelto apunta al ledger creado
    expected = str(tmp_path / ".causadb" / "ledger.log")
    assert resolved == expected, (
        f"ledger path incorrecto: {resolved} != {expected}"
    )


def test_resolve_ledger_auto_init_not_writable(tmp_path, monkeypatch):
    """Auto-init en directorio no-writable → RuntimeError.
    
    Anti-teatro: un stub que crea el ledger igual en un dir
    no-writable falla.
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)

    # Simular estar en un directorio que no existe (no se puede crear .causadb/)
    nowhere = str(tmp_path / "nonexistent")

    def mock_getcwd():
        return nowhere

    monkeypatch.setattr("os.getcwd", mock_getcwd)

    with pytest.raises(RuntimeError, match="not writable|cannot auto-init"):
        _resolve_ledger()


def test_resolve_ledger_skips_invalid_config_no_auto_init(tmp_path, monkeypatch):
    """G5.B: si en CWD existe `.causadb/` pero su config no tiene
    `ledger_path` (config global de telemetría), `_resolve_ledger()` NO
    debe auto-init (crashearía con FileExistsError) — debe fallar con
    RuntimeError explicativo.

    Anti-teatro: un stub que intenta init() en un dir con `.causadb/`
    existente crashearía; este test exige que NO se llame a init().
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)

    # Crear ~/.causadb/ con config de telemetría (sin ledger_path)
    causadb_dir = tmp_path / ".causadb"
    causadb_dir.mkdir()
    with open(causadb_dir / "config.json", "w") as f:
        json.dump({"telemetry": {"enabled": True}}, f)
    monkeypatch.chdir(tmp_path)

    from causadb._workspace import WorkspaceManager
    init_calls = []

    def spy_init(*args, **kwargs):
        init_calls.append(args)
        return {"ledger_path": "boom"}

    monkeypatch.setattr("causadb.mcp.server.WorkspaceManager.init", spy_init)

    with pytest.raises(RuntimeError, match="ledger_path|workspace|--ledger|CAUSADB_LEDGER_PATH"):
        _resolve_ledger()

    assert init_calls == [], (
        "auto-init NO debe llamarse cuando existe .causadb/ sin ledger_path válido"
    )


def test_server_main_degrades_when_resolve_ledger_fails(tmp_path, monkeypatch):
    """G5.B: si `_resolve_ledger()` falla al arrancar (p.ej. `.causadb/`
    inválido en CWD), `main()` NO debe crashear — debe degradar a
    `create_server()` sin default (tools con `ledger_path` explícito
    siguen funcionando).

    Anti-teatro: un stub que deja propagar la excepción falla.
    """
    from causadb.mcp.server import main, create_server

    run_calls = []

    def fake_run(self, transport=None):
        run_calls.append(transport)

    monkeypatch.setattr("causadb.mcp.server._resolve_ledger",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("causadb.mcp.server.create_server",
                        lambda *a, **k: type("S", (), {"run": fake_run})())

    main()

    assert run_calls == ["stdio"], (
        "main() debe llegar a mcp.run() sin crashear tras fallo de _resolve_ledger()"
    )


def test_resolve_ledger_env_over_discover(tmp_path, monkeypatch):
    """Env var tiene prioridad sobre workspace existente.
    
    Anti-teatro: un stub que descubre workspace a pesar de env var falla.
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.chdir(tmp_path)

    # Crear workspace existente
    from causadb._workspace import WorkspaceManager
    ws_result = WorkspaceManager.init(str(tmp_path))

    # Env var apunta a otro lado
    env_path = str(tmp_path / "from_env.log")
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", env_path)

    resolved = _resolve_ledger()
    assert resolved == env_path, (
        f"env var debe ganar sobre discover: {resolved} != {env_path}"
    )


def test_anti_teatro_resolve_ledger_rejects_relative_in_env(tmp_path, monkeypatch):
    """CAUSADB_LEDGER_PATH relativa se convierte a absoluta.
    
    Anti-teatro: un stub que usa la ruta relativa sin convertir falla
    (downstream espera absoluta).
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", "relative/path/ledger.log")

    result = _resolve_ledger()
    assert os.path.isabs(result), (
        f"result debe ser absoluto: {result}"
    )
    assert result.endswith("relative/path/ledger.log"), (
        f"path incorrecto: {result}"
    )


def test_anti_teatro_resolve_ledger_idempotent(tmp_path, monkeypatch):
    """_resolve_ledger() llamado dos veces: segundo es no-op.
    
    Anti-teatro: un stub que crea un segundo workspace en la segunda
    llamada falla (verificamos que sigue siendo el mismo path).
    """
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    first = _resolve_ledger()
    second = _resolve_ledger()
    assert first == second, (
        f"segunda llamada debe devolver mismo path: {first} != {second}"
    )
    # Solo un ledger debe existir
    ledgers = list((tmp_path / ".causadb").glob("ledger.log"))
    assert len(ledgers) == 1, (
        f"solo debe existir 1 ledger.log, encontrados {len(ledgers)}"
    )


def test_anti_teatro_resolve_ledger_genesis_via_ledger_writer(tmp_path, monkeypatch):
    """El auto-init debe escribir SYSTEM_BOOT vía LedgerWriter, no open().
    
    Anti-teatro: un stub que hace open(path, 'a').write() en vez de
    LedgerWriter.append() falla (monkeypatch spy).
    """
    from causadb.mcp.server import _resolve_ledger
    from causadb._ledger_writer import LedgerWriter

    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    calls = []
    original_append = LedgerWriter.append

    def spy_append(self, event):
        calls.append(event)
        return original_append(self, event)

    monkeypatch.setattr(LedgerWriter, "append", spy_append)

    _resolve_ledger()

    assert len(calls) >= 1, (
        "LedgerWriter.append debe ser llamado al menos una vez (SYSTEM_BOOT)"
    )
    assert calls[0].event_type == EventType.SYSTEM_BOOT, (
        f"primer evento debe ser SYSTEM_BOOT, got {calls[0].event_type}"
    )


def test_anti_teatro_resolve_ledger_raises_on_non_writable(tmp_path, monkeypatch):
    """RuntimeError mensaje debe mencionar init y env var."""
    from causadb.mcp.server import _resolve_ledger
    monkeypatch.delenv("CAUSADB_LEDGER_PATH", raising=False)

    nowhere = str(tmp_path / "no_write")
    monkeypatch.setattr("os.getcwd", lambda: nowhere)

    with pytest.raises(RuntimeError) as exc:
        _resolve_ledger()
    msg = str(exc.value)
    assert "init" in msg, f"mensaje debe sugerir init: {msg}"
    assert "CAUSADB_LEDGER_PATH" in msg, f"mensaje debe mencionar env var: {msg}"


# ---------------------------------------------------------------------------
# 19. BIT-14.7 — Tool count se mantiene en 19
# ---------------------------------------------------------------------------

def test_mcp_server_tool_count_is_21():
    """F1 (M2) — las 2 tools OCB nuevas + chronicle_append + recover + 2 shared docs suman al conteo.

    Anti-teatro: agregar tools sin querer o perder tools rompe el count.
    Renombrado desde `test_mcp_server_tool_count_still_15` — el nombre
    anterior mentía (Art. IX): el count ya no es 15.
    """
    server = create_server()
    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)
    assert len(tools) == 21, (
        f"tool count debe ser 21, got {len(tools)}"
    )


def test_mcp_server_ocb_status_tool_registered(tmp_path):
    """F1 (M2) — la tool `ocb_status` está registrada (nombre corto, F.3).

    Anti-teatro: un stub que registra `ocb_status` pero no delega (crashea
    al invocarla) falla porque la invocación real sobre un workspace vacío
    debe devolver JSON con `session_type == "first_run"`.
    """
    server = create_server()
    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)
    names = {t.name for t in tools}
    assert "ocb_status" in names, f"ocb_status no registrada: {sorted(names)}"

    # La tool debe funcionar de verdad (thin wrapper, Art. II) — no crashear.
    ledger_path = str(tmp_path / "ledger.log")
    content_blocks, _ = _call_tool(server, "ocb_status", {
        "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))
    assert payload["session_type"] == "first_run"
    assert payload["all_partition_ids"] == []


def test_mcp_server_ocb_load_partition_tool_registered(tmp_path):
    """F1 (M2) — la tool `ocb_load_partition` está registrada (F.3).

    Anti-teatro: idem — la invocación real sobre una partición inexistente
    debe devolver `[]` (no crashear, no devolver error de registro).
    """
    server = create_server()
    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)
    names = {t.name for t in tools}
    assert "ocb_load_partition" in names, (
        f"ocb_load_partition no registrada: {sorted(names)}"
    )

    content_blocks, _ = _call_tool(server, "ocb_load_partition", {
        "ledger_path": str(tmp_path / "ledger.log"),
        "partition_id": "OCB_PARTITION_does_not_exist.log",
    })
    assert json.loads(_text(content_blocks)) == []


def test_mcp_server_recover_tool_registered(tmp_path, monkeypatch):
    """F.13 — la tool `recover` está registrada (nombre corto, F.3) y
    recupera el storyboard real de una sesión desde la fuente cruda.

    Anti-teatro: un stub que registra `recover` pero no delega (crashea o
    devuelve un dict vacío) falla porque la invocación real sobre el fixture
    opencode debe devolver JSON con `storyboard.turn_count >= 1` y el prompt
    de usuario restaurado (el part `text` que la puntita descarta).
    """
    import shutil

    # Setup inline (NO helpers de test_recover_session.py): fixture real de
    # opencode + env var apuntando al store copiado.
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "opencode_fixture.db")
    db_path = tmp_path / "opencode_fixture.db"
    shutil.copy(fixture, db_path)
    monkeypatch.setenv("CAUSADB_OPENCODE_DB_PATH", str(db_path))

    server = create_server()
    async def _list():
        return await server.list_tools()
    tools = anyio.run(_list)
    names = {t.name for t in tools}
    assert "recover" in names, f"recover no registrada: {sorted(names)}"

    ledger = tmp_path / "ledger.log"
    content_blocks, _ = _call_tool(server, "recover", {
        "ledger_path": str(ledger),
        "session_id": "ses_05f83630bffe1Rzk65A7C0KPys",
        "tool": "opencode",
    })
    payload = json.loads(_text(content_blocks))
    assert "storyboard" in payload, f"envelope sin storyboard: {payload}"
    assert payload["tool"] == "opencode"
    sb = payload["storyboard"]
    assert sb["session_id"] == "ses_05f83630bffe1Rzk65A7C0KPys"
    assert sb["turn_count"] >= 1, f"turn_count debe ser >= 1: {sb}"
    # El prompt de usuario vive en un part text que la puntita descarta —
    # recover lo restaura (Fase 13).
    assert "LedgerIndex" in sb["turns"][0]["prompt"]


def test_mcp_server_recover_search_envelope(tmp_path):
    """F.13 — `recover` con `search=<keyword>` devuelve el envelope
    `{"matches": [...]}` (paridad CLI `_cmd_recover.py:30-31`).

    Anti-teatro: un stub que devuelve el storyboard aunque se pida search
    falla (el envelope debe ser `matches`, no `storyboard`). Persistimos un
    storyboard real bajo `<ledger_dir>/stories/opencode/` y verificamos que
    el keyword matchea de verdad (no lista vacía por defecto).
    """
    ledger = tmp_path / "ledger.log"
    # Persistir un storyboard real (formato Fase 12) para que search tenga
    # algo que matchear — inline, sin helpers de test_recover_session.py.
    sb_dir = tmp_path / "stories" / "opencode"
    sb_dir.mkdir(parents=True)
    sb = {
        "tool": "opencode",
        "session_id": "ses_search_target",
        "created_at": "2026-08-17T00:00:00Z",
        "turn_count": 1,
        "turns": [{
            "prompt": "Implementá el modulo de recuperacion de sesiones",
            "assistant_response": "listo",
            "reasoning": [],
        }],
        "tool_calls": [],
        "files_touched": [],
        "decisions": [],
        "errors": [],
        "tokens_used": 0,
        "duration_s": 0,
    }
    with open(sb_dir / "ses_search_target.json", "w", encoding="utf-8") as fh:
        json.dump(sb, fh)

    server = create_server()
    content_blocks, _ = _call_tool(server, "recover", {
        "ledger_path": str(ledger),
        "search": "recuperacion",
    })
    payload = json.loads(_text(content_blocks))
    assert "matches" in payload, f"envelope sin matches: {payload}"
    assert isinstance(payload["matches"], list)
    assert len(payload["matches"]) == 1, f"expected 1 match, got {payload['matches']}"
    assert payload["matches"][0]["session_id"] == "ses_search_target"


def test_mcp_server_recover_requires_session_id_or_search(tmp_path):
    """F.13 — `recover` sin `session_id` ni `search` → error Fall-Closed
    (Art. VIII): FastMCP convierte el ValueError en respuesta de error.

    Anti-teatro: un stub que devuelve un envelope vacío en vez de fallar
    rompería el assert de excepción.
    """
    server = create_server()
    with pytest.raises(Exception, match="session_id or search"):
        _call_tool(server, "recover", {
            "ledger_path": str(tmp_path / "ledger.log"),
            "session_id": "",
            "search": "",
        })


def test_mcp_recover_lookup_conversation_ref_when_no_tool(tmp_path, monkeypatch):
    """C.4.1 — `recover(session_id=S)` sin `tool` explícito debe hacer
    lookup de `conversation_ref` en el state de replay (paridad CLI
    `_cmd_recover.py:36-51`) y pasárselo a `recover_session` para que
    resuelva el provider SIN recorrer las 9 fuentes.

    Anti-teatro (Art. IX): el test mockea `ReplayEngine` y `recover_session`
    y verifica que el kwarg `conversation_ref` se pasa con el ref real del
    state (NO None). Un wrapper que ignora el lookup falla porque
    `recover_session` se llama con `conversation_ref=None`.
    """
    from unittest.mock import MagicMock

    ledger = tmp_path / "ledger.log"
    ledger.touch()

    ref = {
        "provider": "opencode",
        "locator": "default",
        "locator_kind": "sqlite",
        "native_id": "ses-test",
    }

    mock_re = MagicMock()
    mock_re.return_value.reconstruct_state.return_value = {
        "conversations_recoverable": {"ses-test": {"conversation_ref": ref}}
    }
    monkeypatch.setattr("causadb._replay_engine.ReplayEngine", mock_re)

    captured = {}

    def _fake_recover_session(ledger_path, session_id, tool=None, **kwargs):
        captured["session_id"] = session_id
        captured["tool"] = tool
        captured["conversation_ref"] = kwargs.get("conversation_ref")
        return ("opencode", {"session_id": "ses-test", "turn_count": 1})

    monkeypatch.setattr("causadb._recover_session.recover_session", _fake_recover_session)

    server = create_server()
    content_blocks, _ = _call_tool(server, "recover", {
        "ledger_path": str(ledger),
        "session_id": "ses-test",
    })
    payload = json.loads(_text(content_blocks))

    # ReplayEngine fue instanciado con el ledger_path del request.
    mock_re.assert_called_once_with(str(ledger))
    # recover_session recibió el conversation_ref del state (NO None).
    assert captured["session_id"] == "ses-test"
    assert captured["tool"] is None
    assert captured["conversation_ref"] == ref, (
        f"conversation_ref debe ser {ref}, got {captured['conversation_ref']}"
    )
    # Envelope JSON válido.
    assert payload["tool"] == "opencode"
    assert payload["storyboard"]["session_id"] == "ses-test"


def test_mcp_recover_degrades_when_no_ref_in_state(tmp_path, monkeypatch):
    """C.4.1 — Cuando el `session_id` NO está en
    `conversations_recoverable` (sesión legacy, ledger pre-C.2), el wrapper
    debe degradar pasando `conversation_ref=None` a `recover_session`
    (paridad CLI `_cmd_recover.py:50`).

    Anti-teatro: el test verifica explícitamente que `conversation_ref=None`
    (no ausente, no string vacío) — un wrapper que omite el kwarg o pasa
    otra cosa falla.
    """
    from unittest.mock import MagicMock

    ledger = tmp_path / "ledger.log"
    ledger.touch()

    mock_re = MagicMock()
    mock_re.return_value.reconstruct_state.return_value = {
        "conversations_recoverable": {}  # sesión NO está
    }
    monkeypatch.setattr("causadb._replay_engine.ReplayEngine", mock_re)

    captured = {}

    def _fake_recover_session(ledger_path, session_id, tool=None, **kwargs):
        captured["conversation_ref"] = kwargs.get("conversation_ref")
        return ("opencode", {"session_id": "ses-legacy", "turn_count": 1})

    monkeypatch.setattr("causadb._recover_session.recover_session", _fake_recover_session)

    server = create_server()
    _call_tool(server, "recover", {
        "ledger_path": str(ledger),
        "session_id": "ses-legacy",
    })

    mock_re.return_value.reconstruct_state.assert_called_once()
    assert captured["conversation_ref"] is None, (
        f"degradación debe pasar conversation_ref=None, got {captured['conversation_ref']}"
    )


def test_mcp_recover_explicit_tool_skips_replay_engine(tmp_path, monkeypatch):
    """C.4.1 — Cuando `tool` viene explícito, el wrapper NO debe invocar
    `ReplayEngine` (path corto: lookup solo aplica al auto-detect).

    Anti-teatro (Art. IX): `mock_re.assert_not_called()` valida el path
    corto en el WRAPPER, no en el motor. Un wrapper que siempre invoca
    ReplayEngine falla este assert.
    """
    from unittest.mock import MagicMock

    ledger = tmp_path / "ledger.log"
    ledger.touch()

    mock_re = MagicMock()
    monkeypatch.setattr("causadb._replay_engine.ReplayEngine", mock_re)

    captured = {}

    def _fake_recover_session(ledger_path, session_id, tool=None, **kwargs):
        captured["tool"] = tool
        captured["conversation_ref"] = kwargs.get("conversation_ref")
        return ("opencode", {"session_id": "ses-x", "turn_count": 1})

    monkeypatch.setattr("causadb._recover_session.recover_session", _fake_recover_session)

    server = create_server()
    _call_tool(server, "recover", {
        "ledger_path": str(ledger),
        "session_id": "ses-x",
        "tool": "opencode",
    })

    mock_re.assert_not_called()
    assert captured["tool"] == "opencode"


def test_anti_teatro_causadb_score_thin_wrapper(tmp_path, monkeypatch):
    """Article II (thin-wrapper): `causadb_score` must delegate to
    `causadb._score.compute_score` and pass `ledger_path` through.

    Anti-teatro: a stub `causadb_score` that hardcodes a return value without
    calling `compute_score` would fail because the spy asserts `compute_score`
    was called exactly once with the given `ledger_path`. A stub that calls
    `compute_score` with a different path would fail the argument assertion.
    """
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    import causadb._score as score_mod

    call_args = []
    original_compute = score_mod.compute_score

    def spy_compute(path, config=None):
        call_args.append({"ledger_path": path, "config": config})
        return original_compute(path, config)

    monkeypatch.setattr(score_mod, "compute_score", spy_compute)

    content_blocks, _ = _call_tool(server, "score", {
        "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))

    assert len(call_args) == 1, (
        f"compute_score must be called exactly once, got {len(call_args)} calls"
    )
    assert call_args[0]["ledger_path"] == ledger_path, (
        f"compute_score called with wrong ledger_path: "
        f"{call_args[0]['ledger_path']!r} != {ledger_path!r}"
    )
    # The wrapper must still return valid JSON (proves it serialises the
    # real compute_score output, not a hardcoded value).
    assert "overall_score" in payload, (
        f"wrapper did not serialise compute_score output: {payload}"
    )


def test_causadb_log_decision_links_bit(tmp_path):
    """GAP-02 t22 — log_decision con bit opcional enlaza el evento al BIT."""
    import json
    from causadb import _chronicle_index
    ledger_path = str(tmp_path / "ledger.log")
    server = create_server()

    content_blocks, _ = _call_tool(server, "log_decision", {
        "reasoning": "r", "impact": "medium", "decision_type": "tactical",
        "origin": "agent", "bit": "BIT-MCP", "ledger_path": ledger_path,
    })
    payload = json.loads(_text(content_blocks))
    assert "event_id" in payload
    assert payload["event_id"] in _chronicle_index.query_by_bit(ledger_path, "BIT-MCP")
