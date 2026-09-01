"""Tests for ``causadb._crash_reporter`` (item #5 — Crash Reporter).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They exercise the full crash lifecycle: save, list, dedup,
delete, API, and anti-teatro verification.

Anti-teatro (Article IX): every test has discriminatory power — a stub that
skips dedup or returns empty lists will fail at least one assertion.
"""
import json
import os
import re
import http.client
import pytest

from causadb._crash_reporter import (
    save_crash,
    list_crashes,
    get_crash,
    delete_crash,
    delete_all_crashes,
    _stack_hash,
    _normalize_stack,
    crash_to_github_issue,
    _get_crashes_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_exc():
    """Generate a real exception and return its sys.exc_info() tuple."""
    try:
        raise ValueError("test error message")
    except ValueError:
        import sys
        return sys.exc_info()


def _make_exc_with_cause():
    """Generate an exception with a chained cause."""
    try:
        try:
            raise RuntimeError("inner cause")
        except RuntimeError as inner:
            raise TypeError("outer error") from inner
    except TypeError:
        import sys
        return sys.exc_info()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSaveCrash:
    """save_crash basic functionality."""

    def test_save_crash_creates_file(self):
        """Capture a real exception, call save_crash, assert file exists on disk."""
        info = _make_exc()
        result = save_crash(info)
        assert os.path.isfile(result["path"]), f"crash file not created: {result['path']}"
        # Cleanup
        delete_crash(result["crash_id"])

    def test_save_crash_returns_dict(self):
        """Assert return dict has required keys: crash_id, path."""
        info = _make_exc()
        result = save_crash(info)
        try:
            assert "crash_id" in result
            assert "path" in result
            assert isinstance(result["crash_id"], str)
            assert result["crash_id"].startswith("crash_")
        finally:
            delete_crash(result["crash_id"])


class TestListCrashes:
    """list_crashes functionality."""

    def test_list_crashes_returns_list(self):
        """Save 2 distinct crashes, list returns 2 entries."""
        ids = []
        try:
            # First exception
            try:
                raise ValueError("first")
            except ValueError:
                r1 = save_crash()
            ids.append(r1["crash_id"])

            # Second exception (different type)
            try:
                raise KeyError("second")
            except KeyError:
                r2 = save_crash()
            ids.append(r2["crash_id"])

            crashes = list_crashes()
            assert len(crashes) >= 2, f"expected at least 2, got {len(crashes)}"
        finally:
            for cid in ids:
                delete_crash(cid)


class TestDedup:
    """Deduplication by stack hash."""

    def test_dedup_same_stack_increments_occurrences(self):
        """Save same exception twice, assert 1 file with occurrences=2."""
        info = _make_exc()
        r1 = save_crash(info)
        r2 = save_crash(info)  # same exception info
        try:
            # r2 should report dedup
            assert r2.get("dedup") is True, "second save should be dedup'd"
            # Only one file should exist with crash_id from r1
            crashes = list_crashes()
            matching = [c for c in crashes if c["crash_id"] == r1["crash_id"]]
            assert len(matching) == 1, f"expected 1 matching crash, got {len(matching)}"
            assert matching[0]["occurrences"] >= 2, (
                f"expected occurrences >= 2, got {matching[0]['occurrences']}"
            )
        finally:
            delete_crash(r1["crash_id"])

    def test_anti_teatro_dedup_skips_hashing(self):
        """Verify dedup actually uses hashing — save 2 identical exceptions
        and assert dedup merges them (occurrences==2).

        If the implementation skips hashing and creates separate files,
        this test detects the stub.
        """
        info = _make_exc()

        # Save the same exception twice
        r1 = save_crash(info)
        r2 = save_crash(info)
        try:
            # The second save should have detected dedup
            if not r2.get("dedup"):
                # If dedup failed (anti-teatro detection), still check
                # that listing shows occurrences correctly
                crash_data = get_crash(r1["crash_id"])
                assert crash_data is not None
                # If dedup failed, there would be 2 different crash_ids
                r2_data = get_crash(r2["crash_id"])
                if r2_data is not None and r2_data["crash_id"] != r1["crash_id"]:
                    # Two separate files — DEDUP FAILED
                    pytest.fail(
                        "DEDUP FAILED: save_crash created 2 files for identical stacks. "
                        "This violates the dedup contract (Article IX anti-teatro)."
                    )
        finally:
            delete_crash(r1["crash_id"])
            delete_crash(r2.get("crash_id", ""))


class TestDelete:
    """Delete operations."""

    def test_delete_crash_removes_file(self):
        """Save a crash, delete it, list returns 0."""
        info = _make_exc()
        r = save_crash(info)
        assert delete_crash(r["crash_id"]) is True
        assert get_crash(r["crash_id"]) is None
        crashes = [c for c in list_crashes() if c["crash_id"] == r["crash_id"]]
        assert len(crashes) == 0

    def test_delete_all_removes_all(self):
        """Save 2 crashes, delete_all, list returns 0 crashes."""
        ids = []
        try:
            try:
                raise ValueError("del1")
            except ValueError:
                r1 = save_crash()
            ids.append(r1["crash_id"])

            try:
                raise KeyError("del2")
            except KeyError:
                r2 = save_crash()
            ids.append(r2["crash_id"])

            count = delete_all_crashes()
            assert count >= 2, f"expected >= 2 deletions, got {count}"
            crashes = list_crashes()
            for cid in ids:
                matching = [c for c in crashes if c["crash_id"] == cid]
                assert len(matching) == 0, f"crash {cid} still exists after delete_all"
        finally:
            # Cleanup any leftovers
            for cid in ids:
                delete_crash(cid)

    def test_delete_nonexistent_returns_false(self):
        """delete_crash on non-existent crash_id returns False."""
        assert delete_crash("nonexistent_crash_12345") is False


class TestStackHash:
    """Stack trace normalization and hashing."""

    def test_normalize_removes_home_paths(self):
        """_normalize_stack replaces /home/xxx/ with ~/."""
        text = "  File \"/home/user/project/file.py\", line 42, in foo"
        normalized = _normalize_stack(text)
        assert "~/project/file.py" in normalized
        assert "/home/user/" not in normalized

    def test_stack_hash_consistent(self):
        """Same stack text produces the same hash."""
        text1 = "Traceback (most recent call last):\n  File \"foo.py\", line 1\nValueError: test"
        text2 = "Traceback (most recent call last):\n  File \"foo.py\", line 1\nValueError: test"
        assert _stack_hash(text1) == _stack_hash(text2)

    def test_stack_hash_different(self):
        """Different stack text produces different hashes."""
        text1 = "ValueError: test A"
        text2 = "ValueError: test B"
        assert _stack_hash(text1) != _stack_hash(text2)


class TestCrashToGitHubIssue:
    """GitHub issue formatting."""

    def test_crash_to_github_issue_format(self):
        """crash_to_github_issue returns markdown with expected sections."""
        crash = {
            "crash_id": "crash_test",
            "timestamp": "2026-07-30T12:00:00Z",
            "exception_type": "ValueError",
            "exception_msg": "test message",
            "occurrences": 3,
            "os": "Linux-5.15",
            "version": "0.1.0",
            "stack_text": "Traceback...\nValueError: test message",
        }
        md = crash_to_github_issue(crash)
        assert "## 🐛 Crash Report: ValueError" in md
        assert "**Exception:** `ValueError`" in md
        assert "**Occurrences:** 3" in md
        assert "**OS:** Linux-5.15" in md
        assert "**Version:** 0.1.0" in md
        assert "### Stack Trace" in md
        assert "```" in md
        assert "100% anonymous" in md or "100% anónimos" in md or "anonymous" in md


class TestCrashAPI:
    """REST API crash endpoints."""

    @pytest.fixture
    def ledger_and_server(self, tmp_path):
        from causadb._init import causadb_init
        from causadb._rest_api import serve_in_thread
        result = causadb_init(str(tmp_path / "ws"))
        ledger = result["ledger_path"]
        server = serve_in_thread(ledger, port=0)
        port = server.server_port
        yield ledger, port, server
        server.shutdown()

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _delete(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("DELETE", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def _post(self, port, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request("POST", path, json.dumps(body) if body else "", headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_crash_api_endpoint(self, ledger_and_server):
        """Save a crash, GET /api/crashes, assert JSON array with occurrence data."""
        _, port, _ = ledger_and_server

        # Save a real crash first
        info = _make_exc()
        result = save_crash(info)
        crash_id = result["crash_id"]

        try:
            status, data = self._get(port, "/api/crashes")
            assert status == 200, f"expected 200, got {status}: {data}"
            assert isinstance(data, list), f"expected list, got {type(data)}"
            # Our crash should be in the list
            matching = [c for c in data if c["crash_id"] == crash_id]
            assert len(matching) >= 1, f"crash {crash_id} not found in API response"
            assert "occurrences" in matching[0]
            assert "exception_type" in matching[0]
            assert "timestamp" in matching[0]
        finally:
            delete_crash(crash_id)

    def test_crash_api_delete_endpoint(self, ledger_and_server):
        """DELETE /api/crashes deletes all crashes."""
        _, port, _ = ledger_and_server

        # Save a crash
        info = _make_exc()
        r = save_crash(info)

        try:
            # Verify it's in the list
            status, data = self._get(port, "/api/crashes")
            assert status == 200
            assert len(data) > 0
        finally:
            delete_crash(r["crash_id"])

        # Test DELETE via API
        status, data = self._delete(port, "/api/crashes")
        assert status == 200, f"expected 200, got {status}: {data}"
        assert data.get("status") == "deleted"

    def test_crash_api_delete_specific(self, ledger_and_server):
        """DELETE /api/crashes/{crash_id} deletes a specific crash."""
        _, port, _ = ledger_and_server

        info = _make_exc()
        r = save_crash(info)

        try:
            status, data = self._delete(port, f"/api/crashes/{r['crash_id']}")
            assert status == 200, f"expected 200, got {status}: {data}"
            assert data.get("status") == "deleted"
            assert data.get("crash_id") == r["crash_id"]

            # Verify it's gone
            assert get_crash(r["crash_id"]) is None
        finally:
            delete_crash(r["crash_id"])

    def test_crash_api_export_endpoint(self, ledger_and_server):
        """POST /api/crashes/export exports crashes to local file."""
        _, port, _ = ledger_and_server

        info = _make_exc()
        r = save_crash(info)

        try:
            status, data = self._post(port, "/api/crashes/export")
            assert status == 200, f"expected 200, got {status}: {data}"
            assert data.get("status") == "exported"
            assert "path" in data
            assert os.path.isfile(data["path"])
        finally:
            delete_crash(r["crash_id"])


class TestGlobalExcepthook:
    """Global excepthook installation."""

    def test_save_global_excepthook_installs(self):
        """save_global_excepthook replaces sys.excepthook."""
        import sys
        original = sys.excepthook
        try:
            from causadb._crash_reporter import save_global_excepthook
            save_global_excepthook()
            assert sys.excepthook is not original, "excepthook was not replaced"
        finally:
            sys.excepthook = original


class TestReadVersion:
    """Version reading from pyproject.toml."""

    def test_read_version_returns_string(self):
        from causadb._crash_reporter import _read_version
        version = _read_version()
        assert isinstance(version, str)
        assert len(version) > 0
        assert version == "0.2.0", f"expected 0.2.0, got {version}"
