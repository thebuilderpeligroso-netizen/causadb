"""End-to-end MCP client tests for CausaDB (P.16).

These tests validate that an EXTERNAL MCP agent (opencode, Claude Code, etc.)
can drive the 3 CausaDB tools (`log`, `replay`,
`sentinel`) over a REAL JSON-RPC stdio transport — no mocks of the
nucleus, no in-process shortcuts.

Transport: Sub-option A — `mcp.client.stdio.stdio_client` spawns the server
as a real subprocess via `python -m causadb.mcp.server`. This is the SAME
transport an external agent like Claude Code would use, so it exercises the
full framing / handshake / tool-dispatch path. `causadb` is NOT pip-installed
in the venv, so `PYTHONPATH` is set explicitly on `StdioServerParameters` to
point at the source root.

Async pattern: `anyio.run(...)` inside sync test functions (matches P.15 —
`pytest-asyncio` is NOT installed; `anyio` is available transitively via
`mcp`).

Anti-teatro (Article IX): every test has discriminatory power —
  * `test_e2e_init_log_replay_sentinel` reads `ledger.log` directly and
    verifies the exact event_id persisted + hash chain valid + replay
    `last_hash` matches the log response + sentinel `summary == "OK"`.
    A stub that returns canned JSON without touching the ledger fails all
    three of those assertions.
  * `test_e2e_concurrent_logs_hash_chain_valid` fires 10 concurrent
    `log` calls and asserts `LedgerValidator.validate_chain().is_valid`.
    A stub that bypasses `LedgerWriter`'s lock + fsync corrupts the ledger
    under concurrency and the validator catches it.
  * `test_e2e_determinism_after_multiple_logs` calls `replay` twice
    and asserts byte-identical JSON output (Article VI). A non-deterministic
    replay engine produces different strings.
"""
import json
import os

import anyio
import pytest

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

from causadb._init import causadb_init
from causadb._ledger_validator import LedgerValidator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VENV_PYTHON = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/venv/bin/python"
SOURCE_ROOT = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/causadb"


def _server_params(extra_env=None):
    """Build `StdioServerParameters` for the real CausaDB MCP server subprocess.

    `causadb` is NOT pip-installed in the venv (verified at P.16 startup:
    `pip show causadb` → Package not found), so we MUST set `PYTHONPATH` to
    the source root (the dir containing the `causadb/` package). The env dict
    is built fresh per call so subprocess env mutations don't leak across
    tests.

    *extra_env* is merged on top of ``os.environ`` for tests that need the
    server to read specific config from the environment (e.g.
    ``CAUSADB_WORKSPACE_DIR`` to exercise the auto-snapshot path).
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = SOURCE_ROOT
    if extra_env:
        env.update(extra_env)
    return StdioServerParameters(
        command=VENV_PYTHON,
        args=["-m", "causadb.mcp.server"],
        env=env,
    )


def _text_from_result(result):
    """Concatenate `.text` from all TextContent blocks in a CallToolResult."""
    return "".join(getattr(b, "text", str(b)) for b in result.content)


def _file_modified_event(path="/foo/bar.py", action="create", source="opencode:agent1"):
    return json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx-e2e",
        "source": source,
        "source_type": "agent",
        "payload": {"path": path, "action": action},
    })


def _command_run_event(command="pytest tests/", source="opencode:agent1"):
    return json.dumps({
        "event_type": "COMMAND_RUN",
        "ctx_id": "ctx-e2e",
        "source": source,
        "source_type": "agent",
        "payload": {"command": command, "exit_code": 0},
    })


def _commit_made_event(commit_hash="abc123def456", source="opencode:agent1"):
    return json.dumps({
        "event_type": "COMMIT_MADE",
        "ctx_id": "ctx-e2e",
        "source": source,
        "source_type": "agent",
        "payload": {"commit_hash": commit_hash, "message": "wip"},
    })


# ---------------------------------------------------------------------------
# 1. init → log → replay → sentinel over a real subprocess
# ---------------------------------------------------------------------------

def test_e2e_init_log_replay_sentinel(tmp_path):
    """Full external-agent lifecycle over real stdio JSON-RPC.

    Sequence:
      1. `causadb_init` directly (setup step — not an MCP tool).
      2. `causadb_log` via MCP client → assert `CallToolResult.isError is False`
         and content JSON has `event_id`, `hash`, `timestamp`.
      3. Read `ledger.log` directly → assert 1 entry beyond genesis, hash
         chain valid, and the persisted event_id matches the MCP response.
      4. `causadb_replay` via MCP client → assert `files_modified` len 1,
         `events_applied >= 2` (genesis + FILE_MODIFIED), and `last_hash`
         equals the hash returned by step 2.
      5. `causadb_sentinel` via MCP client → assert `summary == "OK"` on a
         fresh valid ledger.

    Anti-teatro: a stub `causadb_log` returning canned JSON without writing
    fails step 3 (ledger has 0 entries beyond genesis / event_id mismatch).
    A stub `causadb_replay` returning canned state fails step 4
    (`last_hash` won't match the real hash from step 2). A stub
    `causadb_sentinel` returning canned `"OK"` would pass this test alone,
    but steps 2-4 already pin the real ledger, so the sentinel call is
    exercising the real `evaluate_rules` against that ledger.
    """
    workspace = str(tmp_path / "ws")
    init_result = causadb_init(workspace, config=None)
    ledger_path = init_result["ledger_path"]
    assert os.path.exists(ledger_path), "causadb_init must create ledger.log"

    async def scenario():
        params = _server_params()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # 2. causadb_log via MCP
                log_result = await session.call_tool("log", {
                    "event_json": _file_modified_event(),
                    "ledger_path": ledger_path,
                })
                assert log_result.isError is False, (
                    f"causadb_log returned error: {_text_from_result(log_result)}"
                )
                log_payload = json.loads(_text_from_result(log_result))
                assert "event_id" in log_payload, f"missing event_id: {log_payload}"
                assert "hash" in log_payload, f"missing hash: {log_payload}"
                assert "timestamp" in log_payload, f"missing timestamp: {log_payload}"
                assert log_payload["event_id"], "event_id must be non-empty"
                assert log_payload["hash"], "hash must be non-empty"
                logged_hash = log_payload["hash"]
                logged_event_id = log_payload["event_id"]

                # 4. causadb_replay via MCP (called before step 3 read so the
                #    subprocess is still alive; step 3 read happens after the
                #    `async with` block closes — but we need the replay result
                #    inside the session).
                replay_result = await session.call_tool("replay", {
                    "ledger_path": ledger_path,
                })
                assert replay_result.isError is False, (
                    f"causadb_replay returned error: {_text_from_result(replay_result)}"
                )
                replay_state = json.loads(_text_from_result(replay_result))

                # 5. causadb_sentinel via MCP
                sentinel_result = await session.call_tool("sentinel", {
                    "ledger_path": ledger_path,
                })
                assert sentinel_result.isError is False, (
                    f"causadb_sentinel returned error: {_text_from_result(sentinel_result)}"
                )
                sentinel_report = json.loads(_text_from_result(sentinel_result))

                return logged_event_id, logged_hash, replay_state, sentinel_report

    logged_event_id, logged_hash, replay_state, sentinel_report = anyio.run(scenario)

    # 3. Read ledger.log directly and verify persistence + hash chain.
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 2, (
        f"expected 2 entries (genesis + 1 FILE_MODIFIED), got {len(lines)}"
    )
    # Hash chain valid (genesis + 1 event).
    chain_result = LedgerValidator(ledger_path).validate_chain()
    assert chain_result.is_valid, (
        f"hash chain invalid: {chain_result.failure_type} at {chain_result.position}"
    )
    # The persisted event_id matches what the MCP tool returned.
    last_entry = json.loads(lines[-1])
    assert last_entry["event"]["event_id"] == logged_event_id, (
        f"persisted event_id {last_entry['event']['event_id']!r} != "
        f"MCP-returned {logged_event_id!r}"
    )
    assert last_entry["hash"] == logged_hash, (
        f"persisted hash {last_entry['hash']!r} != MCP-returned {logged_hash!r}"
    )

    # 4. Replay assertions.
    assert "files_modified" in replay_state, f"missing files_modified: {replay_state}"
    assert "events_applied" in replay_state, f"missing events_applied: {replay_state}"
    assert "last_hash" in replay_state, f"missing last_hash: {replay_state}"
    assert len(replay_state["files_modified"]) == 1, (
        f"expected 1 file modified, got {len(replay_state['files_modified'])}"
    )
    assert replay_state["events_applied"] >= 2, (
        f"expected events_applied >= 2 (genesis + FILE_MODIFIED), "
        f"got {replay_state['events_applied']}"
    )
    assert replay_state["last_hash"] == logged_hash, (
        f"replay last_hash {replay_state['last_hash']!r} != "
        f"log hash {logged_hash!r}"
    )
    assert replay_state["files_modified"][0]["path"] == "/foo/bar.py"

    # 5. Sentinel assertions — fresh valid ledger → all rules pass.
    assert sentinel_report["summary"] == "OK", (
        f"expected summary 'OK' on fresh valid ledger, got "
        f"{sentinel_report['summary']!r}"
    )
    assert sentinel_report["all_rules_pass"] is True, (
        f"expected all_rules_pass True, got {sentinel_report['all_rules_pass']}"
    )


# ---------------------------------------------------------------------------
# 2. 10 concurrent causadb_log calls → hash chain still valid
# ---------------------------------------------------------------------------

def test_e2e_concurrent_logs_hash_chain_valid(tmp_path):
    """10 concurrent `causadb_log` calls over a single MCP client session.

    Uses `anyio.create_task_group()` + `tg.start_soon(...)` to fire all 10
    calls concurrently against the SAME subprocess session. After all
    return, reads `ledger.log` directly and asserts:
      * Exactly 10 new entries + genesis = 11 total lines.
      * `LedgerValidator(ledger_path).validate_chain().is_valid is True`.

    Anti-teatro: a stub `causadb_log` that bypasses `LedgerWriter`'s
    `threading.Lock` + `fcntl.flock` + `fsync` would interleave partial
    writes / lose updates / corrupt the hash chain under concurrency. The
    `LedgerValidator.validate_chain()` assertion catches any of those
    failure modes (CORRUPTION / CONTINUITY_BREAK / HASH_MISMATCH).
    """
    workspace = str(tmp_path / "ws")
    init_result = causadb_init(workspace, config=None)
    ledger_path = init_result["ledger_path"]

    async def scenario():
        params = _server_params()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                results = []
                errors = []

                async def log_one(i):
                    try:
                        r = await session.call_tool("log", {
                            "event_json": _file_modified_event(
                                path=f"/concurrent/file_{i}.py",
                                action="create",
                            ),
                            "ledger_path": ledger_path,
                        })
                        results.append(r)
                    except Exception as e:
                        errors.append(e)

                async with anyio.create_task_group() as tg:
                    for i in range(10):
                        tg.start_soon(log_one, i)

                assert not errors, f"{len(errors)} concurrent calls raised: {errors}"
                assert len(results) == 10, (
                    f"expected 10 results, got {len(results)}"
                )
                for r in results:
                    assert r.isError is False, (
                        f"concurrent causadb_log returned error: "
                        f"{_text_from_result(r)}"
                    )

    anyio.run(scenario)

    # Read ledger.log directly and verify count + hash chain.
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 11, (
        f"expected 11 entries (genesis + 10 concurrent), got {len(lines)}"
    )

    chain_result = LedgerValidator(ledger_path).validate_chain()
    assert chain_result.is_valid, (
        f"hash chain invalid after 10 concurrent logs: "
        f"{chain_result.failure_type} at position {chain_result.position} "
        f"— {chain_result.description}"
    )


# ---------------------------------------------------------------------------
# 3. Determinism — two replays produce byte-identical state (Article VI)
# ---------------------------------------------------------------------------

def test_e2e_determinism_after_multiple_logs(tmp_path):
    """Two `causadb_replay` calls produce byte-identical JSON (Article VI).

    Logs 5 events sequentially via MCP `causadb_log` (mix of FILE_MODIFIED,
    COMMAND_RUN, COMMIT_MADE — valid payloads per SchemaValidator rules),
    then calls `causadb_replay` twice via MCP and asserts the two JSON
    strings are byte-identical AND that the parsed states have the same
    `files_modified` length, `commands_run` length, `commits_made` length,
    `last_hash`, and `events_applied`.

    # Article VI — deterministic replay: same ledger → same state, always.

    Anti-teatro: a non-deterministic replay engine (e.g., one that injects
    wall-clock `datetime.now()` into the reconstructed state, or shuffles
    list ordering) would fail the byte-identical assertion. The per-field
    assertions catch subtler non-determinism (e.g., same string but
    different field counts due to a race).
    """
    workspace = str(tmp_path / "ws")
    init_result = causadb_init(workspace, config=None)
    ledger_path = init_result["ledger_path"]

    events_to_log = [
        _file_modified_event(path="/det/a.py", action="create"),
        _command_run_event(command="pytest tests/"),
        _commit_made_event(commit_hash="deadbeef"),
        _file_modified_event(path="/det/b.py", action="modify"),
        _command_run_event(command="ruff check ."),
    ]

    async def scenario():
        params = _server_params()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Log 5 events sequentially.
                for ev_json in events_to_log:
                    r = await session.call_tool("log", {
                        "event_json": ev_json,
                        "ledger_path": ledger_path,
                    })
                    assert r.isError is False, (
                        f"causadb_log error: {_text_from_result(r)}"
                    )

                # First replay.
                replay1_result = await session.call_tool("replay", {
                    "ledger_path": ledger_path,
                })
                assert replay1_result.isError is False, (
                    f"first causadb_replay error: {_text_from_result(replay1_result)}"
                )
                state1_str = _text_from_result(replay1_result)

                # Second replay.
                replay2_result = await session.call_tool("replay", {
                    "ledger_path": ledger_path,
                })
                assert replay2_result.isError is False, (
                    f"second causadb_replay error: {_text_from_result(replay2_result)}"
                )
                state2_str = _text_from_result(replay2_result)

                return state1_str, state2_str

    state1_str, state2_str = anyio.run(scenario)

    # Article VI — byte-identical replay output.
    assert state1_str == state2_str, (
        f"Article VI violation: two replays produced different JSON.\n"
        f"state1 (first 500 chars): {state1_str[:500]!r}\n"
        f"state2 (first 500 chars): {state2_str[:500]!r}"
    )

    state1 = json.loads(state1_str)
    state2 = json.loads(state2_str)

    # Per-field determinism (catches subtler bugs even if strings match by
    # accident — e.g., a future refactor that changes field names but keeps
    # string length equal).
    assert len(state1["files_modified"]) == len(state2["files_modified"]), (
        f"files_modified length differs: {len(state1['files_modified'])} vs "
        f"{len(state2['files_modified'])}"
    )
    assert len(state1["commands_run"]) == len(state2["commands_run"]), (
        f"commands_run length differs: {len(state1['commands_run'])} vs "
        f"{len(state2['commands_run'])}"
    )
    assert len(state1["commits_made"]) == len(state2["commits_made"]), (
        f"commits_made length differs: {len(state1['commits_made'])} vs "
        f"{len(state2['commits_made'])}"
    )
    assert state1["last_hash"] == state2["last_hash"], (
        f"last_hash differs: {state1['last_hash']!r} vs {state2['last_hash']!r}"
    )
    assert state1["events_applied"] == state2["events_applied"], (
        f"events_applied differs: {state1['events_applied']} vs "
        f"{state2['events_applied']}"
    )

    # Sanity: the 5 logged events + genesis = 6 events applied.
    assert state1["events_applied"] == 6, (
        f"expected 6 events applied (genesis + 5), got {state1['events_applied']}"
    )
    assert len(state1["files_modified"]) == 2, (
        f"expected 2 FILE_MODIFIED, got {len(state1['files_modified'])}"
    )
    assert len(state1["commands_run"]) == 2, (
        f"expected 2 COMMAND_RUN, got {len(state1['commands_run'])}"
    )
    assert len(state1["commits_made"]) == 1, (
        f"expected 1 COMMIT_MADE, got {len(state1['commits_made'])}"
    )


# ---------------------------------------------------------------------------
# 4. Regression: auto-snapshot via MCP server must NOT deadlock on fork
#    (BIT-CHR.57 — fork → spawn fix). The server's `log` tool triggers
#    `_maybe_auto_snapshot` when the event payload contains `writes`. Under
#    the OLD `fork` context, the snapshot worker deadlocked inside the
#    FastMCP/anyio event loop (~5s timeout → `_snapshot_disabled=True` →
#    pre/post permanently None). Under `spawn` the worker bootstraps a
#    fresh interpreter without inheriting the parent's locks and finishes
#    well under the 5s timeout, so pre/post snapshots ARE populated.
# ---------------------------------------------------------------------------

def test_e2e_log_with_writes_auto_snapshots_via_mcp(tmp_path):
    """B.2 / BIT-CHR.57 regression — an external agent logs a FILE_MODIFIED
    event with ``writes`` over the real MCP stdio server; the server reads
    ``CAUSADB_WORKSPACE_DIR`` from its env and auto-takes pre/post snapshots.

    Anti-teatro: under the regresssed `fork` context, the snapshot worker
    deadlocked inside the event loop and the MCP `log` call hung until the
    5s `_SNAPSHOT_TIMEOUT` elapsed, then permanently disabled snapshots
    (``pre_snapshot``/``post_snapshot`` left None). With the `spawn` fix the
    worker completes well under the timeout and BOTH snapshots land.

    A stub that just persists the event without taking snapshots fails
    `assert pre_snapshot is not None`.
    """
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    # File that the `writes` entry refers to.
    target = os.path.join(workspace, "main.py")
    with open(target, "w") as f:
        f.write("v1\n")

    # `causadb_init` creates a fresh dir; we use a SEPARATE dir for the
    # ledger so the workspace (above) stays free of ledger scaffolding
    # that would pollute the snapshot.
    init_dir = str(tmp_path / "init")
    init_result = causadb_init(init_dir, config=None)
    ledger_path = init_result["ledger_path"]
    # The server reads ``CAUSADB_WORKSPACE_DIR`` from its env (to snapshot)
    # and the ledger path comes via the ``log`` tool argument.
    server_env = {
        "CAUSADB_WORKSPACE_DIR": workspace,
        "CAUSADB_BLOB_STORE_ENABLED": "true",
    }

    def _writes_event():
        return json.dumps({
            "event_type": "FILE_MODIFIED",
            "ctx_id": "ctx-e2e-snap",
            "source": "opencode:agent1",
            "source_type": "agent",
            "payload": {
                "path": target,
                "action": "modified",
                "writes": ["main.py"],
            },
        })

    async def scenario():
        params = _server_params(extra_env=server_env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                log_result = await session.call_tool("log", {
                    "event_json": _writes_event(),
                    "ledger_path": ledger_path,
                })
                assert log_result.isError is False, (
                    f"causadb_log returned error: "
                    f"{_text_from_result(log_result)}"
                )
                log_payload = json.loads(_text_from_result(log_result))
                assert log_payload.get("event_id"), \
                    f"missing event_id: {log_payload}"

    anyio.run(scenario)

    # Read the ledger directly and check the persisted payload has BOTH
    # pre and post snapshots populated.
    with open(ledger_path) as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert len(lines) >= 2, "ledger must have genesis + the logged event"
    entry = json.loads(lines[-1])
    payload = entry["event"].get("payload", {})
    assert payload.get("pre_snapshot") is not None, (
        "auto-snapshot via MCP must populate pre_snapshot — if this is None "
        "the spawn fix regressed back to the fork deadlock"
    )
    assert payload.get("post_snapshot") is not None, (
        "auto-snapshot via MCP must populate post_snapshot — if this is None "
        "the spawn fix regressed back to the fork deadlock"
    )
    assert (payload["pre_snapshot"]
            != payload["post_snapshot"]), (
        "post snapshot must differ from pre when the workspace changed"
    )
