"""Tests for #11 — Federación de ledgers (hub-and-spoke sync).

Covers:
    - SyncEngine: init, configure, state persistence, read helpers
    - Push/pull: no-hub guard, empty push, connection error, full sync
    - CLI integration: status, config, push
    - Anti-teatro: no silent failures on unreachable hub
    - Node identity stability
"""

import json
import os
import hashlib
import socket
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from causadb._sync import SyncEngine, SyncError
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType


# ======================================================================
# Helpers
# ======================================================================


class FakeHTTPResponse:
    """Simple mock HTTP response that works as a context manager.

    This avoids all MagicMock pitfalls with ``with`` statements and
    ``json.loads(resp.read())`` patterns.
    """

    def __init__(self, body_bytes: bytes = b""):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _fake_response(body_bytes: bytes) -> FakeHTTPResponse:
    """Create a ``FakeHTTPResponse`` with the given body."""
    return FakeHTTPResponse(body_bytes)


def _create_ledger(ledger_path: str, count: int = 3):
    """Create a ledger at ``ledger_path`` with genesis + ``count`` events.

    Writes a genesis event (seq=0) first, then ``count`` real events
    (seq 1 .. count). This matches the real-world sequence layout from
    ``causadb init``.
    """
    writer = LedgerWriter(ledger_path)

    # Genesis
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT,
        ctx_id="genesis",
        source="causadb:init",
        source_type="human",
        payload={"action": "init"},
        metadata=EventMetadata(trace_id="init", session_id="init"),
    )
    writer.append(genesis)

    # Real events
    for i in range(count):
        ev = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="sync-test",
            source="test:sync",
            payload={"path": f"file_{i}.txt", "content": f"data_{i}"},
            metadata=EventMetadata(session_id="sync-test-session"),
        )
        writer.append(ev)
    return writer


def _read_ledger_lines(ledger_path: str):
    """Read all JSON lines from a ledger file."""
    if not os.path.isfile(ledger_path):
        return []
    with open(ledger_path) as f:
        return [json.loads(line.strip()) for line in f if line.strip()]


def _raise_connection_error(req, *a, **kw):
    """Mock helper that raises URLError (connection refused)."""
    raise URLError("Connection refused")


def _raise_http_error(code: int = 500, msg: str = "Error"):
    """Return a mock helper that raises HTTPError with given code."""
    def _raiser(req, *a, **kw):
        raise HTTPError(
            getattr(req, "full_url", str(req)),
            code, msg, {}, None,
        )
    return _raiser


def _fake_urlopen_generator(responses: dict):
    """Return a ``urlopen`` mock that routes by URL pattern.

    ``responses`` maps URL substrings to one of:
        - ``bytes`` — response body (200)
        - ``(bytes, int)`` — response body + HTTP status
        - callable — invoked with ``(req, *a, **kw)``; should return
          ``FakeHTTPResponse`` or raise an exception.
    """

    def mock_urlopen(req, *a, **kw):
        url = str(getattr(req, "full_url", req))
        for pattern, value in responses.items():
            if pattern in url:
                if callable(value):
                    # Callable — may raise or return
                    return value(req, *a, **kw)
                if isinstance(value, tuple):
                    body, status = value
                    if status >= 400:
                        raise HTTPError(url, status, "Error", {}, None)
                    return _fake_response(body)
                return _fake_response(value)
        # No match → connection reset / unreachable
        raise URLError(f"No mock for {url}")

    return mock_urlopen


def _run_cli(main_func, args, capsys):
    """Run CLI main() with args, return (exit_code, stdout)."""
    rc = main_func(args=args)
    captured = capsys.readouterr()
    return rc, captured.out


# ======================================================================
# Core SyncEngine tests (6)
# ======================================================================


class TestSyncEngineCore:
    """Direct unit tests for SyncEngine (no network)."""

    def test_sync_engine_init(self, tmp_path):
        """SyncEngine stores ledger_path and config_dir."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        assert engine.ledger_path == ledger
        assert engine.config_dir == str(tmp_path)
        assert engine._state_path().endswith("sync_state.json")

    def test_configure_save_state(self, tmp_path):
        """configure() writes state to sync_state.json on disk."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-api-key", interval_minutes=30)

        state_path = engine._state_path()
        assert os.path.isfile(state_path)

        with open(state_path) as f:
            state = json.load(f)

        assert state["hub_url"] == "http://hub:8080"
        assert state["api_key"] == "test-api-key"
        assert state["interval_minutes"] == 30
        assert state["last_synced_seq"] == 0

    def test_configure_trailing_slash_stripped(self, tmp_path):
        """configure() strips trailing slash from hub_url."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080/", "key", 60)
        state = engine._load_state()
        assert state["hub_url"] == "http://hub:8080"

    def test_get_config(self, tmp_path):
        """get_config() returns metadata but never the api_key value."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "secret-key-12345", interval_minutes=120)

        cfg = engine.get_config()
        assert cfg["hub_url"] == "http://hub:8080"
        assert cfg["has_api_key"] is True
        assert "secret-key-12345" not in str(cfg)
        assert cfg["last_synced_seq"] == 0
        assert cfg["interval_minutes"] == 120
        assert cfg["ledger_path"] == ledger

    def test_read_events_from_sequence(self, tmp_path):
        """_read_events_from(N) returns only entries with seq > N."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=3)  # genesis(0) + 3 events(1,2,3)

        engine = SyncEngine(ledger)

        events_after_0 = engine._read_events_from(0)
        assert len(events_after_0) == 3  # seq 1, 2, 3

        events_after_1 = engine._read_events_from(1)
        assert len(events_after_1) == 2  # seq 2, 3

        events_after_3 = engine._read_events_from(3)
        assert len(events_after_3) == 0

    def test_get_last_sequence(self, tmp_path):
        """_get_last_sequence() returns 0 for empty ledger, correct seq after."""
        ledger = str(tmp_path / "empty_ledger.log")
        engine = SyncEngine(ledger)

        # Empty ledger → 0
        assert engine._get_last_sequence() == 0

        # After genesis + 5 events (seq 0..5)
        _create_ledger(ledger, count=5)
        assert engine._get_last_sequence() == 5  # 6 entries, last seq=5

        # Separate ledger: genesis + 2 events (seq 0..2)
        ledger2 = str(tmp_path / "empty2.log")
        _create_ledger(ledger2, count=2)
        engine2 = SyncEngine(ledger2)
        assert engine2._get_last_sequence() == 2  # 3 entries, last seq=2


# ======================================================================
# Push / Pull tests (5)
# ======================================================================


class TestSyncPushPull:
    """Test push and pull operations with mocked HTTP."""

    def test_push_no_hub_configured(self, tmp_path):
        """push() without hub config raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        with pytest.raises(SyncError, match="Hub URL not configured"):
            engine.push()

    def test_pull_no_hub_configured(self, tmp_path):
        """pull() without hub config raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        with pytest.raises(SyncError, match="Hub URL not configured"):
            engine.pull()

    def test_push_empty_no_new_events(self, tmp_path):
        """push() with empty ledger returns pushed=0 without hitting network."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        result = engine.push()
        assert result["pushed"] == 0
        assert result["status"] == "no_new_events"

    def test_push_hub_connection_error(self, tmp_path):
        """push() when hub URLError raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": _raise_connection_error,
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub connection failed"):
                engine.push()

    def test_push_hub_http_error(self, tmp_path):
        """push() with hub HTTP 401 raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "bad-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": _raise_http_error(401, "Unauthorized"),
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub push failed.*401"):
                engine.push()

    def test_pull_hub_http_403(self, tmp_path):
        """pull() with hub HTTP 403 raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "bad-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/pull": _raise_http_error(403, "Forbidden"),
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub pull failed.*403"):
                engine.pull()


# ======================================================================
# Full sync tests (3)
# ======================================================================


class TestSyncFull:
    """Test full_sync coordination of push and pull."""

    def test_full_sync_coordinates_push_pull(self, tmp_path):
        """full_sync() calls push then pull, returning combined result."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=2)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b'{"accepted": 2, "new_last_seq": 2}',
            "/sync/pull": b'{"events": [], "last_seq": 2}',
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            result = engine.full_sync()

        assert "push" in result
        assert "pull" in result
        assert "timestamp" in result
        assert result["push"]["pushed"] == 2
        assert result["pull"]["pulled"] == 0

    def test_full_sync_with_remote_events(self, tmp_path):
        """full_sync() imports remote events from hub into local ledger."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        remote_event = {
            "event": {
                "event_id": "remote-1111-2222-3333",
                "event_type": "FILE_MODIFIED",
                "timestamp": "2026-07-30T12:00:00Z",
                "ctx_id": "remote",
                "source": "remote:node2",
                "parent_event_id": None,
                "source_type": "agent",
                "schema_version": "0.1.0",
                "payload": {"path": "remote_file.txt"},
                "metadata": None,
                "pre_snapshot": None,
                "post_snapshot": None,
                "sequence_number": 100,
            },
            "prev_hash": "abc",
            "hash": "def",
        }

        pull_body = json.dumps({"events": [remote_event], "last_seq": 100}).encode()

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b'{"accepted": 1, "new_last_seq": 1}',
            "/sync/pull": pull_body,
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            result = engine.full_sync()

        assert result["push"]["pushed"] == 1
        assert result["pull"]["pulled"] == 1
        assert result["pull"]["total_remote"] == 1

        # Verify the remote event was written to the ledger
        lines = _read_ledger_lines(ledger)
        event_ids = [ln["event"]["event_id"] for ln in lines]
        assert "remote-1111-2222-3333" in event_ids

    def test_full_sync_both_empty(self, tmp_path):
        """full_sync() with empty ledger returns zero counts."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b'{"accepted": 0, "new_last_seq": 0}',
            "/sync/pull": b'{"events": [], "last_seq": 0}',
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            result = engine.full_sync()

        assert result["push"]["pushed"] == 0
        assert result["pull"]["pulled"] == 0


# ======================================================================
# CLI integration tests (3)
# ======================================================================


class TestSyncCli:
    """CLI integration tests — ``causadb sync ...``."""

    def test_cli_sync_status_not_configured(self, tmp_path, capsys):
        """``causadb sync status`` without config shows configured=False."""
        from causadb.cli.main import main

        rc_init, _ = _run_cli(main, ["init", str(tmp_path / "ws")], capsys)
        assert rc_init == 0

        ledger = str(tmp_path / "ws/.causadb/ledger.log")
        rc, output = _run_cli(main, [
            "sync", "status", "--ledger", ledger,
        ], capsys)
        assert rc == 0

        data = json.loads(output)
        assert data["configured"] is False
        assert "(not configured)" in data["hub_url"]

    def test_cli_sync_config(self, tmp_path, capsys):
        """``causadb sync config --hub-url`` saves config and status shows it."""
        from causadb.cli.main import main

        rc_init, _ = _run_cli(main, ["init", str(tmp_path / "ws")], capsys)
        assert rc_init == 0

        ledger = str(tmp_path / "ws/.causadb/ledger.log")

        rc, output = _run_cli(main, [
            "sync", "config",
            "--hub-url", "http://my-hub:9999",
            "--api-key", "cli-test-key",
            "--interval", "15",
            "--ledger", ledger,
        ], capsys)
        assert rc == 0
        data = json.loads(output)
        assert data["status"] == "configured"
        assert data["hub_url"] == "http://my-hub:9999"

        # Verify via status
        rc2, out2 = _run_cli(main, ["sync", "status", "--ledger", ledger], capsys)
        assert rc2 == 0
        status = json.loads(out2)
        assert status["configured"] is True
        assert status["hub_url"] == "http://my-hub:9999"

    def test_cli_sync_push_with_mock(self, tmp_path, capsys):
        """``causadb sync push`` with mocked hub returns JSON result."""
        from causadb.cli.main import main

        # Init workspace
        ledger = str(tmp_path / "ws/.causadb/ledger.log")
        rc_init, _ = _run_cli(main, ["init", str(tmp_path / "ws")], capsys)
        assert rc_init == 0

        # Add one event
        writer = LedgerWriter(ledger)
        ev = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="cli-test",
            source="test:cli",
            payload={"path": "test.txt"},
        )
        writer.append(ev)

        # Configure sync
        rc_cfg, _ = _run_cli(main, [
            "sync", "config",
            "--hub-url", "http://mock-hub:8080",
            "--api-key", "mock-key",
            "--ledger", ledger,
        ], capsys)
        assert rc_cfg == 0

        # Mock HTTP and call push
        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b'{"accepted": 1, "new_last_seq": 1}',
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            rc, output = _run_cli(main, [
                "sync", "push", "--ledger", ledger,
            ], capsys)

        assert rc == 0
        data = json.loads(output)
        assert data["pushed"] == 1
        assert "hub_response" in data


# ======================================================================
# Anti-teatro tests (3)
# ======================================================================


class TestAntiTeatro:
    """Tests that protect against regression to stub/mock behavior.

    Artículo VIII: no stubs.
    Artículo IX: Fall-Closed on failure.
    """

    def test_anti_teatro_sync_no_silent_fail(self, tmp_path):
        """push() raises on unreachable hub — never silently returns OK."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": _raise_connection_error,
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub connection failed"):
                engine.push()

    def test_anti_teatro_pull_no_silent_fail(self, tmp_path):
        """pull() raises on unreachable hub — never silently returns OK."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/pull": _raise_connection_error,
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub connection failed"):
                engine.pull()

    def test_anti_teatro_no_empty_events_written(self, tmp_path):
        """Pull with empty event list does not alter ledger."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        lines_before = len(_read_ledger_lines(ledger))

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b'{"accepted": 0, "new_last_seq": 0}',
            "/sync/pull": b'{"events": [], "last_seq": 0}',
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            engine.full_sync()

        lines_after = len(_read_ledger_lines(ledger))
        assert lines_after == lines_before, (
            f"Ledger grew from {lines_before} to {lines_after} despite empty pull"
        )


# ======================================================================
# Node identity tests (1)
# ======================================================================


class TestNodeIdentity:
    """Node ID stability."""

    def test_node_id_is_stable(self):
        """Same hostname always produces the same node_id."""
        host = socket.gethostname()
        expected_hash = hashlib.sha256(host.encode()).hexdigest()[:12]
        expected = f"node-{expected_hash}"

        node1 = SyncEngine._get_node_id()
        node2 = SyncEngine._get_node_id()
        node3 = SyncEngine._get_node_id()

        assert node1 == expected
        assert node2 == node1
        assert node3 == node1
        assert node1.startswith("node-")
        assert len(node1) == 17  # "node-" + 12 hex chars


# ======================================================================
# Error handling tests (3)
# ======================================================================


class TestErrorHandling:
    """Error paths beyond basics."""

    def test_push_hub_http_500(self, tmp_path):
        """push() on hub HTTP 500 raises SyncError with status code."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": _raise_http_error(500, "Internal Error"),
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError) as exc:
                engine.push()

        assert "500" in str(exc.value)

    def test_pull_malformed_response(self, tmp_path):
        """pull() with non-JSON hub response raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/pull": b"this is not json",
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub response error"):
                engine.pull()

    def test_push_malformed_response(self, tmp_path):
        """push() with non-JSON hub response raises SyncError."""
        ledger = str(tmp_path / "ledger.log")
        _create_ledger(ledger, count=1)
        engine = SyncEngine(ledger)
        engine.configure("http://hub:8080", "test-key")

        mock_urlopen = _fake_urlopen_generator({
            "/sync/push": b"not-json-at-all",
        })

        with patch("urllib.request.urlopen", mock_urlopen):
            with pytest.raises(SyncError, match="Hub response error"):
                engine.push()
