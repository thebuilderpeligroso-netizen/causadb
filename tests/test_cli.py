"""Tests for the `causadb` CLI (P.14).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They exercise the CLI as a thin delegator to the existing
nucleus — no logic is reimplemented here.

Pattern A is used: `main(args=[...]) -> int` returns the exit code and prints
JSON to stdout; tests capture stdout via `capsys` and assert on the returned
int + parsed JSON.

Anti-teatro (Article IX): every test has discriminatory power — a stub CLI
that returns empty dicts or skips validation will fail at least one assertion
in this file.
"""
import json
import os
import hashlib
import sys

import pytest

from causadb.cli.main import main
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args, capsys):
    """Run the CLI with the given args list, return (exit_code, stdout_str)."""
    rc = main(args=args)
    captured = capsys.readouterr()
    return rc, captured.out


# ---------------------------------------------------------------------------
# Portabilidad Windows: fix encoding UTF-8 en main()
# ---------------------------------------------------------------------------

def test_main_reconfigures_streams_to_utf8(monkeypatch):
    """main() fuerza UTF-8 en stdout/stderr (fix portabilidad Windows).

    El emoji ⚡ del revive crashea en consolas cp1252 (UnicodeEncodeError).
    main() debe llamar ``reconfigure(encoding="utf-8")`` sobre stdout y
    stderr al arrancar. Sin el fix, ``calls`` queda vacío → este test
    falla (RED).
    """
    calls = []

    class _Stream:
        def write(self, *a, **k):
            return 0

        def flush(self, *a, **k):
            return None

        def reconfigure(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(sys, "stdout", _Stream())
    monkeypatch.setattr(sys, "stderr", _Stream())

    rc = main(args=[])
    assert rc == 0
    assert any(c.get("encoding") == "utf-8" for c in calls), (
        "main() debe llamar reconfigure(encoding='utf-8') en stdout/stderr"
    )


def test_main_no_crash_without_reconfigure(monkeypatch):
    """main() no lanza AttributeError si stdout/stderr no tienen reconfigure.

    El try/except es OBLIGATORIO: capsys/StringIO y test doubles no tienen
    ``reconfigure``. En esos casos debe ser no-op (no crash).
    """
    class _NoReconfigure:
        def write(self, *a, **k):
            return 0

        def flush(self, *a, **k):
            return None

    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", _NoReconfigure())

    # main() sin subcomando imprime help y retorna 0 — no debe crashear.
    rc = main(args=[])
    assert rc == 0


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def test_cli_init_creates_workspace(tmp_path, capsys):
    """`causadb init <abs>` creates .causadb/ with config + ledger + chronicle."""
    workspace = str(tmp_path / "ws")
    rc, out = _run(["init", workspace], capsys)

    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    # Files must exist on disk (inside .causadb/)
    assert os.path.exists(payload["ledger_path"]), "ledger.log not created"
    assert os.path.exists(payload["chronicle_path"]), "chronicle not created"
    assert os.path.exists(payload["config_path"]), "config.json not created"
    # ledger.log must be non-empty (genesis event written)
    with open(payload["ledger_path"]) as f:
        ledger_content = f.read()
    assert ledger_content.strip() != "", "ledger.log is empty — genesis not written"
    # stdout must be JSON-parseable with the three required keys
    assert "ledger_path" in payload
    assert "chronicle_path" in payload
    assert "config_path" in payload
    assert payload["config_path"].endswith(".causadb/config.json")
    assert payload["chronicle_path"].endswith("CAUSADB_CHRONICLE.md")


def test_cli_init_relative_path_works(tmp_path, capsys):
    """`causadb init relative` → works (abspath conversion)."""
    import os as _os
    cwd = _os.getcwd()
    _os.chdir(tmp_path)
    try:
        rc, out = _run(["init", "myproject"], capsys)
        assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
        payload = json.loads(out)
        assert "ledger_path" in payload
        assert _os.path.exists(payload["ledger_path"])
    finally:
        _os.chdir(cwd)


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

def test_cli_log_appends_event(tmp_path, capsys):
    """`causadb log <json>` appends an event; ledger + hash chain valid."""
    # Set up: init a workspace first
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    init_payload = json.loads(out_init)
    ledger_path = init_payload["ledger_path"]

    # Count entries before (genesis = 1)
    with open(ledger_path) as f:
        before = len([ln for ln in f if ln.strip()])
    assert before == 1, f"genesis should be 1 entry, got {before}"

    event_json = json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "source_type": "agent",
        "payload": {"path": "/foo", "action": "create"},
        "metadata": {"trace_id": "t", "session_id": "s"},
    })

    rc, out = _run(["log", "--ledger", ledger_path, event_json], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"

    # (a) stdout JSON-parseable with required keys
    payload = json.loads(out)
    assert "event_id" in payload
    assert "hash" in payload
    assert "timestamp" in payload
    assert payload["event_id"], "event_id must be non-empty"
    assert payload["hash"], "hash must be non-empty"

    # (b) ledger.log now has exactly 2 entries (genesis + new)
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 2, f"expected 2 entries, got {len(lines)}"

    # (c) hash chain valid: hash == sha256(event_json_sorted + prev_hash)
    new_entry = json.loads(lines[1])
    genesis_entry = json.loads(lines[0])
    prev_hash = genesis_entry["hash"]
    event_dict = new_entry["event"]
    event_json_sorted = json.dumps(event_dict, sort_keys=True)
    expected_hash = hashlib.sha256((event_json_sorted + prev_hash).encode()).hexdigest()
    assert new_entry["hash"] == expected_hash, (
        f"hash chain broken: expected {expected_hash}, got {new_entry['hash']}"
    )
    assert new_entry["prev_hash"] == prev_hash
    assert new_entry["hash"] == payload["hash"]


def test_cli_log_invalid_json_raises(tmp_path, capsys):
    """`causadb log 'not json'` → exit != 0 and error present."""
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]

    rc, out = _run(["log", "--ledger", ledger_path, "not json"], capsys)
    assert rc != 0, f"expected non-zero exit, got {rc}"
    payload = json.loads(out)
    assert "error" in payload
    assert payload["error"], "error message must be non-empty"


def test_cli_log_metadata_priority_accepted(tmp_path, capsys):
    """(BIT-CHR.35 P1) `causadb log` with `metadata.priority` must NOT fail
    with "Invalid metadata" (TypeError from `EventMetadata(**data["metadata"])`).

    Anti-teatro: against the old code this returns exit 1 with
    error_type=TypeError. After the fix it exits 0, appends the event, and the
    event read back from the ledger preserves `priority`.
    """
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]

    event_json = json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "source_type": "agent",
        "payload": {"path": "/foo", "action": "create"},
        "metadata": {"trace_id": "t", "session_id": "s", "priority": "high"},
    })
    rc, out = _run(["log", "--ledger", ledger_path, event_json], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "event_id" in payload
    assert "error" not in payload

    # Read back from the ledger (Ledger Monism) — priority must survive
    from causadb._ledger_reader import LedgerReader
    events = list(LedgerReader(ledger_path).read_all())
    assert events[-1].metadata is not None
    assert events[-1].metadata.priority == "high"
    assert events[-1].metadata.session_id == "s"


def test_cli_log_invalid_event_schema_raises(tmp_path, capsys):
    """Anti-teatro: FILE_MODIFIED without `path` must be rejected by the CLI
    via `validate_event_schema` BEFORE appending (Fall-Closed). A stub CLI
    that skips schema validation would let this through and fail this test.
    """
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]

    # FILE_MODIFIED requires {path, action}; we omit `path`.
    event_json = json.dumps({
        "event_type": "FILE_MODIFIED",
        "ctx_id": "ctx",
        "source": "opencode:agent1",
        "source_type": "agent",
        "payload": {"action": "create"},  # missing `path`
    })

    rc, out = _run(["log", "--ledger", ledger_path, event_json], capsys)
    assert rc != 0, (
        f"expected non-zero exit for invalid schema, got {rc}; stdout={out!r}"
    )
    payload = json.loads(out)
    assert "error" in payload
    # The error must mention schema validation
    assert "schema" in payload["error"].lower() or "Schema" in payload["error"], (
        f"error must reference schema validation, got: {payload['error']!r}"
    )

    # Anti-teatro: ledger must NOT have gained a new entry
    with open(ledger_path) as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1, (
        f"invalid event must NOT be appended; expected 1 entry, got {len(lines)}"
    )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def test_cli_replay_outputs_state(tmp_path, capsys):
    """`causadb replay --ledger <path>` emits JSON state reflecting the ledger."""
    ledger_path = str(tmp_path / "ledger.log")
    # Pre-populate directly via LedgerWriter (bypass CLI for setup)
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload={"path": "/foo", "action": "create"},
    ))

    rc, out = _run(["replay", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"

    state = json.loads(out)
    # Required state keys present
    assert "files_modified" in state
    assert "events_applied" in state
    # The FILE_MODIFIED event must be reflected
    assert len(state["files_modified"]) == 1, (
        f"expected 1 file modified, got {len(state['files_modified'])}"
    )
    assert state["files_modified"][0]["path"] == "/foo"
    # event_count: genesis not in this ledger (we used LedgerWriter directly,
    # no init), so events_applied == 1 for the single FILE_MODIFIED.
    assert state["events_applied"] == 1, (
        f"expected events_applied=1, got {state['events_applied']}"
    )


# ---------------------------------------------------------------------------
# sentinel
# ---------------------------------------------------------------------------

def test_cli_sentinel_outputs_report(tmp_path, capsys):
    """`causadb sentinel --ledger <path>` emits JSON report with 3 rules."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload={"path": "/foo", "action": "create"},
    ))

    rc, out = _run(["sentinel", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"

    report = json.loads(out)
    assert "all_rules_pass" in report
    assert "summary" in report
    assert "results" in report
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
# validate
# ---------------------------------------------------------------------------

def test_cli_validate_outputs_result(tmp_path, capsys):
    """`causadb validate --ledger <path>` emits JSON ValidationResult."""
    ledger_path = str(tmp_path / "ledger.log")
    # Set up a valid ledger directly
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent1",
        source_type="agent",
        payload={"path": "/foo", "action": "create"},
    ))

    rc, out = _run(["validate", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"

    result = json.loads(out)
    assert "is_valid" in result
    assert result["is_valid"] is True, (
        f"valid ledger must report is_valid=True, got {result}"
    )
    assert "failure_type" in result
    assert "description" in result


# ---------------------------------------------------------------------------
# no args → help
# ---------------------------------------------------------------------------

def test_cli_no_args_shows_help(capsys):
    """`causadb` with no args → exit 0 and help/usage text on stdout."""
    rc, out = _run([], capsys)
    assert rc == 0, f"expected exit 0 for no-args help, got {rc}"
    # argparse prints help to stdout when --help is triggered; check for
    # common help markers.
    help_markers = ("usage:", "subcommand", "positional arguments", "optional arguments")
    assert any(m in out.lower() for m in help_markers), (
        f"stdout must contain help text, got: {out[:200]!r}"
    )


# ---------------------------------------------------------------------------
# query (F.2.4)
# ---------------------------------------------------------------------------

def test_cli_query_by_event_type(tmp_path, capsys):
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    writer.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="a:b"))
    rc, out = _run(["query", "--ledger", ledger_path, "--event-type", "FILE_MODIFIED"], capsys)
    assert rc == 0
    results = json.loads(out)
    assert len(results) == 1
    assert results[0]["event"]["event_type"] == "FILE_MODIFIED"

def test_causadb_query_no_results_returns_empty_array(tmp_path, capsys):
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="a:b"))
    rc, out = _run(["query", "--ledger", ledger_path, "--event-type", "COMMIT_MADE"], capsys)
    assert rc == 0
    results = json.loads(out)
    assert results == []

def test_causadb_query_by_source(tmp_path, capsys):
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:a"))
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="causadb:v"))
    rc, out = _run(["query", "--ledger", ledger_path, "--source", "opencode:a"], capsys)
    assert rc == 0
    results = json.loads(out)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# feedback (F.5.1)
# ---------------------------------------------------------------------------

def test_cli_feedback_lists_human_feedback(tmp_path, capsys):
    """`causadb feedback --ledger <path>` lista todos los HUMAN_FEEDBACK events
    del ledger como JSON array."""
    import uuid
    # 1. init workspace
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]
    # 2. loguear 1 HUMAN_FEEDBACK event via LedgerWriter
    writer = LedgerWriter(ledger_path)
    event = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload={
            "feedback_type": "approval",
            "target_event_id": str(uuid.uuid4()),
        },
    )
    writer.append(event)
    # 3. invocar CLI feedback
    rc, out = _run(["feedback", "--ledger", ledger_path], capsys)
    # 4. exit_code == 0, output JSON parseable, len == 1
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    results = json.loads(out)
    assert isinstance(results, list)
    assert len(results) == 1, f"expected 1 HUMAN_FEEDBACK event, got {len(results)}"
    assert results[0]["event"]["event_type"] == "HUMAN_FEEDBACK"


# --- F.5.2: SANDBOX_STATE CLI ---

def test_cli_sandbox_lists_violations_and_total(tmp_path, capsys):
    """`causadb sandbox --ledger <path>` debe listar violations + total_mutations."""
    import uuid
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]
    writer = LedgerWriter(ledger_path)
    # Log a violation
    v_event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload={
            "mutation_type": "file_write_outside_workspace",
            "path_or_resource": "/etc/passwd",
            "violates_boundary": True,
        },
    )
    writer.append(v_event)
    # Log a non-violation
    m_event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload={
            "mutation_type": "container_created",
            "path_or_resource": "sandbox",
            "violates_boundary": False,
        },
    )
    writer.append(m_event)
    rc, out = _run(["sandbox", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    data = json.loads(out)
    assert "violations" in data
    assert "total_mutations" in data
    assert len(data["violations"]) == 1
    assert data["total_mutations"] == 2


# --- F.5.5: STREAM_INTERRUPTED CLI ---

def test_cli_stream_lists_interrupted(tmp_path, capsys):
    """`causadb stream --ledger <path>` lista STREAM_INTERRUPTED events como JSON array."""
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    ledger_path = json.loads(out_init)["ledger_path"]
    writer = LedgerWriter(ledger_path)
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload={
            "interrupt_reason": "user_cancel",
            "partial_completion_hash": "d8e9f0",
        },
    )
    writer.append(event)
    rc, out = _run(["stream", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    results = json.loads(out)
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["event"]["event_type"] == "STREAM_INTERRUPTED"


# ---------------------------------------------------------------------------
# F.8 — proxy subcommand (Ollama + LM Studio)
# ---------------------------------------------------------------------------

def test_cli_proxy_ollama_no_api_key(tmp_path, capsys):
    """`causadb proxy --adapter ollama --api-key` is optional → exit 0, JSON with content."""
    from unittest.mock import patch, MagicMock

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "ollama says hi"}}],
        "usage": {"total_tokens": 5},
    }).encode()
    fake_response.__enter__.return_value = fake_response

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    with patch("urllib.request.urlopen", return_value=fake_response):
        rc, out = _run([
            "proxy", "--adapter", "ollama", "--model", "llama3",
            "--prompt", "hi", "--ledger", str(ledger),
        ], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "content" in payload
    assert payload["content"] == "ollama says hi"


def test_cli_proxy_lmstudio_no_api_key(tmp_path, capsys):
    """`causadb proxy --adapter lmstudio` without --api-key → exit 0."""
    from unittest.mock import patch, MagicMock

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "lmstudio says hi"}}],
        "usage": {"total_tokens": 5},
    }).encode()
    fake_response.__enter__.return_value = fake_response

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    with patch("urllib.request.urlopen", return_value=fake_response):
        rc, out = _run([
            "proxy", "--adapter", "lmstudio", "--model", "llama3",
            "--prompt", "hi", "--ledger", str(ledger),
        ], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "content" in payload
    assert payload["content"] == "lmstudio says hi"


def test_cli_proxy_openai_no_api_key_raises(tmp_path, capsys):
    """`causadb proxy --adapter openai` without --api-key → exit 1, error mentions api_key."""
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    rc, out = _run([
        "proxy", "--adapter", "openai", "--model", "gpt-4",
        "--prompt", "hi", "--ledger", str(ledger),
    ], capsys)
    assert rc == 1, f"expected exit 1, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "error" in payload
    assert "api_key" in payload["error"].lower() or "api key" in payload["error"].lower(), (
        f"error should mention api_key, got: {payload['error']!r}"
    )


# ---------------------------------------------------------------------------
# F.9 — mcp-proxy subcommand
# ---------------------------------------------------------------------------

def test_cli_mcp_proxy_help_shows_subcommands(capsys):
    """`causadb mcp-proxy --help` shows start/stop/status."""
    from causadb.cli.main import main
    with pytest.raises(SystemExit):
        main(args=["mcp-proxy", "--help"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "start" in out, f"start must appear in mcp-proxy --help, got:\n{out}"
    assert "stop" in out, f"stop must appear in mcp-proxy --help, got:\n{out}"
    assert "status" in out, f"status must appear in mcp-proxy --help, got:\n{out}"


def test_cli_mcp_proxy_start_without_config_uses_defaults(tmp_path, capsys):
    """`causadb mcp-proxy start` without --config falls back to default paths."""
    from causadb.cli.main import main
    ledger = tmp_path / "ledger.log"
    rc = main(args=["mcp-proxy", "start", "--ledger", str(ledger)])
    captured = capsys.readouterr()
    out = captured.out
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "mode" in payload
    assert payload["mode"] == "log-only"
    assert payload["ledger"] == str(ledger)


def test_cli_mcp_proxy_start_without_ledger_uses_discovery(tmp_path, capsys):
    """`causadb mcp-proxy start` without --ledger auto-discovers workspace."""
    from causadb.cli.main import main
    cwd = os.getcwd()
    try:
        # Init a workspace in tmp_path so discovery finds it
        main(["init", str(tmp_path / "proj")])
        os.chdir(str(tmp_path / "proj"))
        rc = main(args=["mcp-proxy", "start"])
        assert rc == 0
    finally:
        os.chdir(cwd)


def test_cli_mcp_proxy_help_shows_ledger_flag(capsys):
    from causadb.cli.main import main
    with pytest.raises(SystemExit):
        main(args=["mcp-proxy", "start", "--help"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "--ledger" in out


def test_cli_proxy_ollama_choices_appear_in_help(capsys):
    """`causadb proxy --help` shows ollama and lmstudio in choices."""
    from causadb.cli.main import main
    with pytest.raises(SystemExit):
        main(args=["proxy", "--help"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "ollama" in out, f"ollama must appear in proxy --help, got:\n{out}"
    assert "lmstudio" in out, f"lmstudio must appear in proxy --help, got:\n{out}"


# ---------------------------------------------------------------------------
# F.10 — ocb subcommand
# ---------------------------------------------------------------------------

def test_cli_ocb_help_shows_subcommands(capsys):
    """`causadb ocb --help` shows status/close/purge."""
    from causadb.cli.main import main
    with pytest.raises(SystemExit):
        main(args=["ocb", "--help"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "status" in out
    assert "close" in out
    assert "purge" in out


def test_cli_ocb_status_first_run(tmp_path, capsys):
    """`causadb ocb status --ledger <path>` on new workspace."""
    from causadb.cli.main import main
    ledger = tmp_path / "ledger.log"
    rc = main(args=["ocb", "status", "--ledger", str(ledger)])
    captured = capsys.readouterr()
    assert rc == 0, f"expected 0, got {rc}; out={captured.out!r}"
    payload = json.loads(captured.out)
    assert payload["session_type"] == "first_run"


def test_cli_ocb_status_with_partitions(tmp_path, capsys):
    """`causadb ocb status` shows partitions when they exist."""
    from causadb.cli.main import main
    ledger = tmp_path / "ledger.log"
    ocb_base = tmp_path / "ocb"
    ocb_base.mkdir()
    import json as _json
    from causadb._ocb_manager import OCB
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ocb = OCB("test", str(ocb_base), threshold_events=2)
    for _ in range(5):
        ocb.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="test"
        ))
    rc = main(args=["ocb", "status", "--ledger", str(ledger)])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert "partition_ids" in payload or "preloaded_partitions" in payload


def test_cli_ocb_close_generates_summary(tmp_path, capsys):
    """`causadb ocb close --ledger <path>` generates summary."""
    from causadb.cli.main import main
    from causadb._ocb_manager import OCB
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = tmp_path / "ledger.log"
    ocb_base = tmp_path / "ocb"
    ocb_base.mkdir()
    ocb = OCB("test", str(ocb_base))
    ocb.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="test"
    ))
    rc = main(args=["ocb", "close", "--ledger", str(ledger)])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "closed"


def test_cli_ocb_purge_keep_last(tmp_path, capsys):
    """`causadb ocb purge --keep-last <n> --ledger <path>`."""
    from causadb.cli.main import main
    from causadb._ocb_manager import OCB
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = tmp_path / "ledger.log"
    ocb_base = tmp_path / "ocb"
    ocb_base.mkdir()
    ocb = OCB("test", str(ocb_base), threshold_events=2)
    for _ in range(6):
        ocb.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="test"
        ))
    rc = main(args=["ocb", "purge", "--keep-last", "2", "--ledger", str(ledger)])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "purged"


def test_cli_ocb_ledger_required(capsys):
    """`causadb ocb status` without --ledger and without workspace fails (Fall-Closed)."""
    import tempfile
    from causadb.cli.main import main
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            rc = main(args=["ocb", "status"])
            assert rc != 0
        finally:
            os.chdir(cwd)


# ---------------------------------------------------------------------------
# F.13.3.4 — score subcommand
# ---------------------------------------------------------------------------

def _build_score_ledger(ledger_path):
    """Build a ledger with churn + waste for the CLI score tests."""
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.LLM_INVOKED, ctx_id="s1", source="opencode:a",
        source_type="llm",
        payload={"model": "gpt-4", "cost": 0.1, "prompt": "hi"},
        timestamp="2026-01-01T10:00:00Z",
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="s1", source="opencode:a",
        source_type="agent",
        payload={"path": "/foo.py", "action": "create"},
        timestamp="2026-01-01T10:00:05Z",
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="s1", source="opencode:a",
        source_type="agent",
        payload={"path": "/foo.py", "action": "delete"},
        timestamp="2026-01-01T10:01:00Z",
    ))
    # Second session with no waste.
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="s2", source="opencode:a",
        source_type="agent",
        payload={"path": "/bar.py", "action": "create"},
        timestamp="2026-01-01T11:00:00Z",
    ))
    return writer


def test_cli_score_default_format(tmp_path, capsys):
    """`causadb score` → exit 0, output JSON parseable with required keys."""
    ledger_path = str(tmp_path / "ledger.log")
    _build_score_ledger(ledger_path)

    rc, out = _run(["score", "--ledger", ledger_path], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    for key in ("overall_score", "churn_score", "waste_score",
                "survival_score", "weights_used", "correlation_method"):
        assert key in payload, f"missing key {key!r} in score output"
    assert 0.0 <= payload["overall_score"] <= 100.0


def test_cli_score_markdown_format(tmp_path, capsys):
    """`causadb score --format md` → output contains '## Score'."""
    ledger_path = str(tmp_path / "ledger.log")
    _build_score_ledger(ledger_path)

    rc, out = _run(["score", "--ledger", ledger_path, "--format", "md"], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    assert "## Score" in out, f"markdown output must contain '## Score', got: {out[:200]!r}"


def test_cli_score_by_session(tmp_path, capsys):
    """`causadb score --by-session` → output contains per-session scores."""
    ledger_path = str(tmp_path / "ledger.log")
    _build_score_ledger(ledger_path)

    rc, out = _run(["score", "--ledger", ledger_path, "--by-session"], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert "per_session" in payload
    per = payload["per_session"]
    assert "s1" in per, f"session s1 must appear in per_session, got {list(per.keys())}"
    assert "s2" in per, f"session s2 must appear in per_session, got {list(per.keys())}"
    # Each session entry must have an overall_score.
    for ctx, s in per.items():
        assert "overall_score" in s, f"session {ctx} missing overall_score"


def test_cli_score_session_filter(tmp_path, capsys):
    """`causadb score --session CTX_ID` → only that session."""
    ledger_path = str(tmp_path / "ledger.log")
    _build_score_ledger(ledger_path)

    rc, out = _run(["score", "--ledger", ledger_path, "--session", "s1"], capsys)
    assert rc == 0, f"expected exit 0, got {rc}; stdout={out!r}"
    payload = json.loads(out)
    assert payload["session_id"] == "s1"
    assert "overall_score" in payload
    # The filtered output must NOT contain the per_session dict of other sessions.
    assert "per_session" not in payload or "s2" not in payload.get("per_session", {})


# ---------------------------------------------------------------------------
# opencode-config (F.11.5 fix)
# ---------------------------------------------------------------------------

def test_cli_opencode_config_generates_correct_format(tmp_path, capsys):
    """`causadb opencode-config` genera formato opencode correcto."""
    workspace = str(tmp_path / "ws")
    rc_init, out_init = _run(["init", workspace], capsys)
    assert rc_init == 0
    opencode_output = str(tmp_path / "ws" / "causadb.opencode.jsonc")

    rc, out = _run(["opencode-config", "--project", workspace, "--output", opencode_output], capsys)

    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "output_path" in payload
    assert "ledger_path" in payload

    with open(opencode_output) as f:
        data = json.load(f)

    assert "mcp" in data, "Top-level key debe ser 'mcp'"
    assert "mcpServers" not in data, "No debe usar formato Claude Code (mcpServers)"

    mcp = data["mcp"]["causadb"]
    assert mcp["type"] == "local"
    assert isinstance(mcp["command"], list)
    assert len(mcp["command"]) == 1
    assert os.path.basename(mcp["command"][0]) == "causadb-mcp"
    assert "-m" not in mcp["command"]
    assert "causadb.mcp.server" not in mcp["command"]
    assert mcp["enabled"] is True
    assert "CAUSADB_LEDGER_PATH" in mcp["environment"]
    assert isinstance(mcp["environment"]["CAUSADB_LEDGER_PATH"], str)
    assert len(mcp["environment"]["CAUSADB_LEDGER_PATH"]) > 0

    assert "args" not in mcp, "No debe usar command+args (formato Claude Code)"
    assert "disabled" not in mcp, "No debe usar disabled (formato Claude Code)"
    assert "env" not in mcp, "No debe usar env (formato Claude Code)"
    assert "autoApprove" not in mcp, "opencode no soporta autoApprove"


def test_cli_opencode_config_no_workspace_errors(tmp_path, capsys):
    """`causadb opencode-config` sin .causadb/ → exit 1 + error."""
    rc, out = _run(["opencode-config", "--project", str(tmp_path / "nonexistent")], capsys)
    assert rc == 1, f"expected 1, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "error" in payload


def test_cli_score_advertises_correlation_method(tmp_path, capsys):
    """`causadb score` output must mention 'timestamp_proximity' as a warning."""
    ledger_path = str(tmp_path / "ledger.log")
    _build_score_ledger(ledger_path)

    # JSON format must include correlation_method.
    rc, out = _run(["score", "--ledger", ledger_path], capsys)
    assert rc == 0
    payload = json.loads(out)
    assert payload["correlation_method"] == "timestamp_proximity"

    # Markdown format must include the warning text.
    rc, out = _run(["score", "--ledger", ledger_path, "--format", "md"], capsys)
    assert rc == 0
    assert "timestamp_proximity" in out, (
        f"markdown output must mention 'timestamp_proximity', got: {out[:300]!r}"
    )
    # And the warning advertises the imprecision.
    assert "Advertencia" in out or "imprecisa" in out, (
        f"markdown output must advertise the imprecision, got: {out[:300]!r}"
    )

# --- R.3.4: CLI causadb snapshot ---

def test_cli_snapshot_logs_event(tmp_path):
    from causadb.cli.main import main
    from causadb._init import causadb_init
    from causadb._ledger_index import LedgerIndex
    from causadb._event_types import EventType
    
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    
    # Ejecutar CLI
    # Requeridos: --tests (int), --fases (str)
    # Opcionales: --bloqueantes, --notas
    cmd = [
        "snapshot", 
        "--ledger", ledger, 
        "--tests", "10", 
        "--fases", "R.1,R.2",
        "--bloqueantes", "0",
        "--notas", "test snapshot"
    ]
    
    # main retorna exit code
    exit_code = main(cmd)
    assert exit_code == 0
    
    # Verificar evento en ledger usando LedgerReader directamente
    from causadb._ledger_reader import LedgerReader
    reader = LedgerReader(ledger)
    events = [e for e in reader.read_all_entries() if e["event"]["event_type"] == EventType.PROJECT_SNAPSHOT.value]
    assert len(events) == 1
    event = events[0]["event"]
    assert event["payload"]["total_tests"] == 10
    assert event["payload"]["notas"] == "test snapshot"

def test_cli_snapshot_missing_tests_rejects(tmp_path):
    from causadb.cli.main import main
    from causadb._init import causadb_init
    import pytest

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    # Falta --tests
    cmd = [
        "snapshot",
        "--ledger", ledger,
        "--fases", "R.1"
    ]

    # main debería fallar con SystemExit(2)
    with pytest.raises(SystemExit) as e:
        main(cmd)
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# GAP-02 — log --decision --bit, chronicle append auto-link, reconstruct
# ---------------------------------------------------------------------------

def test_cli_log_decision_links_bit(tmp_path, capsys):
    """t17 — `causadb log --decision --bit BIT-X` escribe el
    GOVERNANCE_DECISION y lo enlaza al BIT en el chronicle index."""
    from causadb import _chronicle_index
    ledger = str(tmp_path / "ledger.log")
    rc, out = _run(["log", "--decision", "--reasoning", "decidir X",
                    "--impact", "high", "--decision-type", "architectural",
                    "--origin", "agent", "--bit", "BIT-X",
                    "--ledger", ledger], capsys)
    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "event_id" in payload
    assert payload["event_id"] in _chronicle_index.query_by_bit(ledger, "BIT-X")
    # el evento quedó en el ledger con bit_id en payload (autoridad del ledger)
    with open(ledger) as f:
        entry = json.loads([ln for ln in f if ln.strip()][0])
    assert entry["event"]["event_type"] == "GOVERNANCE_DECISION"
    assert entry["event"]["payload"]["bit_id"] == "BIT-X"


def test_cli_chronicle_append_auto_links(tmp_path, capsys):
    """t18 — `causadb chronicle append --bit BIT-Y` enlaza el evento
    CHRONICLE_ENTRY al BIT automáticamente."""
    from causadb import _chronicle_index
    ledger = str(tmp_path / "ledger.log")
    rc, out = _run(["chronicle", "append", "--bit", "BIT-Y", "--title", "t",
                    "--date", "2026-08-13", "--maker", "m", "--checker", "c",
                    "--summary", "s", "--ledger", ledger], capsys)
    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert payload["event_id"] in _chronicle_index.query_by_bit(ledger, "BIT-Y")


def test_cli_chronicle_reconstruct_append_order(tmp_path, capsys):
    """t19 — `chronicle reconstruct --bit` reconstruye hasta la frontera
    (append-order): B backdated queda EXCLUIDO."""
    from causadb import _chronicle_index
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)

    def _fm(path, ts):
        return writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="test",
            timestamp=ts, payload={"path": path, "action": "create"},
        ))["event"]["event_id"]

    eid_a = _fm("/A", "2026-08-13T10:00:00Z")
    eid_c = _fm("/C", "2026-08-13T10:00:02Z")
    _fm("/B", "2026-08-13T10:00:01Z")  # backdated, apendeado al final
    _chronicle_index.link_events(ledger, "BIT-Z", [eid_c])

    rc, out = _run(["chronicle", "reconstruct", "--bit", "BIT-Z",
                    "--ledger", ledger], capsys)
    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    state = json.loads(out)
    paths = [f["path"] for f in state["files_modified"]]
    assert paths == ["/A", "/C"], f"B debe quedar excluido, got {paths}"


def test_cli_chronicle_reconstruct_multi_id_frontier(tmp_path, capsys):
    """t23 — multi-event_id: la frontera es el de mayor seq de append."""
    from causadb import _chronicle_index
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)

    def _fm(path, ts):
        return writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="test",
            timestamp=ts, payload={"path": path, "action": "create"},
        ))["event"]["event_id"]

    eid_a = _fm("/A", "2026-08-13T10:00:00Z")
    eid_c = _fm("/C", "2026-08-13T10:00:02Z")
    eid_b = _fm("/B", "2026-08-13T10:00:01Z")
    _chronicle_index.link_events(ledger, "BIT-Z", [eid_c, eid_b])

    rc, out = _run(["chronicle", "reconstruct", "--bit", "BIT-Z",
                    "--ledger", ledger], capsys)
    assert rc == 0, f"expected 0, got {rc}; out={out!r}"
    state = json.loads(out)
    paths = [f["path"] for f in state["files_modified"]]
    assert paths == ["/A", "/C", "/B"], f"frontera = mayor seq (B), got {paths}"


def test_cli_chronicle_reconstruct_no_events_fails(tmp_path, capsys):
    """t20 — BIT sin eventos enlazados → error (exit != 0)."""
    ledger = str(tmp_path / "ledger.log")
    rc, out = _run(["chronicle", "reconstruct", "--bit", "BIT-VACIO",
                    "--ledger", ledger], capsys)
    assert rc != 0, f"expected non-zero, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "error" in payload


def test_cli_chronicle_reconstruct_ghost_fails(tmp_path, capsys):
    """t21 — event_id fantasma enlazado (no existe en el ledger) → error."""
    from causadb import _chronicle_index
    ledger = str(tmp_path / "ledger.log")
    _chronicle_index.link_events(ledger, "BIT-GHOST",
                                 ["deadbeef-0000-0000-0000-000000000000"])
    rc, out = _run(["chronicle", "reconstruct", "--bit", "BIT-GHOST",
                    "--ledger", ledger], capsys)
    assert rc != 0, f"expected non-zero, got {rc}; out={out!r}"
    payload = json.loads(out)
    assert "error" in payload
