"""Tests for CausaDB anonymous telemetry (#6, #9).

Covers:
  - Counter increment/reset
  - Opt-out (telemetry_enabled=False → no-op)
  - Weekly report save/export
  - API config endpoints
  - Anti-teatro: no silent collection when disabled
"""
import json
import os
import threading
import pytest


# ---------------------------------------------------------------------------
# 1. _telemetry module tests
# ---------------------------------------------------------------------------

class TestTelemetryCounters:
    """Unit tests for counter logic in causadb._telemetry."""

    def test_increment_increases_counter(self):
        from causadb._telemetry import increment, get_counters, reset_counters
        reset_counters()
        increment("test_counter")
        counters = get_counters()
        assert counters.get("test_counter") == 1

    def test_increment_multiple_times(self):
        from causadb._telemetry import increment, get_counters, reset_counters
        reset_counters()
        increment("multi", 3)
        increment("multi", 2)
        counters = get_counters()
        assert counters.get("multi") == 5

    def test_increment_is_noop_when_disabled(self, monkeypatch):
        from causadb._telemetry import increment, get_counters, reset_counters
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: False)
        reset_counters()
        increment("should_not_count")
        counters = get_counters()
        assert counters.get("should_not_count") is None

    def test_get_counters_thread_safe(self):
        from causadb._telemetry import increment, get_counters, reset_counters
        reset_counters()

        errors = []

        def worker(n):
            try:
                for _ in range(100):
                    increment(f"thread_{n}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread errors: {errors}"
        counters = get_counters()
        total = sum(v for v in counters.values() if str(v).isdigit())
        assert total == 500, f"Expected 500 increments, got {total}"

    def test_reset_counters(self):
        from causadb._telemetry import increment, get_counters, reset_counters
        reset_counters()
        increment("a")
        assert get_counters().get("a") == 1
        reset_counters()
        assert get_counters() == {}


class TestTelemetryWeeklyReport:
    """Tests for weekly report aggregation."""

    def test_save_weekly_report_creates_file(self, tmp_path, monkeypatch):
        from causadb._telemetry import (
            increment, reset_counters, save_weekly_report,
        )
        # Ensure is_enabled returns True regardless of user config state
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: True)
        monkeypatch.setattr("causadb._telemetry._get_telemetry_dir", lambda: str(tmp_path))

        reset_counters()
        increment("dashboard_timeline_opened", 3)
        increment("causadb_score_called")

        report = save_weekly_report()
        assert "counters" in report, f"Expected counters in report, got: {report}"
        assert report["counters"]["dashboard_timeline_opened"] == 3
        assert report["counters"]["causadb_score_called"] == 1
        assert "week" in report
        assert "version" in report
        assert "platform" in report

        # File was created
        files = os.listdir(str(tmp_path))
        week_files = [f for f in files if f.startswith("week_") and f.endswith(".json")]
        assert len(week_files) == 1
        with open(os.path.join(str(tmp_path), week_files[0])) as f:
            saved = json.load(f)
        assert saved["counters"]["dashboard_timeline_opened"] == 3

    def test_save_weekly_report_respects_disable(self, tmp_path, monkeypatch):
        """No file created when telemetry is disabled."""
        from causadb._telemetry import (
            increment, reset_counters, save_weekly_report,
        )
        monkeypatch.setattr("causadb._telemetry._get_telemetry_dir", lambda: str(tmp_path))
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: False)

        reset_counters()
        increment("should_not_persist")
        report = save_weekly_report()
        assert report["status"] == "disabled"

        files = os.listdir(str(tmp_path))
        week_files = [f for f in files if f.startswith("week_")]
        assert len(week_files) == 0

    def test_list_weekly_reports(self, tmp_path, monkeypatch):
        from causadb._telemetry import (
            increment, reset_counters, save_weekly_report, list_weekly_reports,
        )
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: True)
        monkeypatch.setattr("causadb._telemetry._get_telemetry_dir", lambda: str(tmp_path))

        reset_counters()
        increment("test", 1)
        save_weekly_report()

        reports = list_weekly_reports()
        assert len(reports) == 1
        assert reports[0]["counters"]["test"] == 1


class TestTelemetryConfig:
    """Tests for config persistence via CausaDBConfig."""

    def test_config_telemetry_default_enabled(self):
        from causadb._config import CausaDBConfig
        cfg = CausaDBConfig(ledger_path="/tmp/test/ledger.log")
        assert cfg.telemetry_enabled is True

    def test_config_telemetry_from_env(self, monkeypatch):
        from causadb._config import CausaDBConfig
        monkeypatch.setenv("CAUSADB_TELEMETRY_ENABLED", "false")
        monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/test/ledger.log")
        cfg = CausaDBConfig.from_env()
        assert cfg.telemetry_enabled is False

    def test_config_telemetry_from_env_true(self, monkeypatch):
        from causadb._config import CausaDBConfig
        monkeypatch.setenv("CAUSADB_TELEMETRY_ENABLED", "true")
        monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/test/ledger.log")
        cfg = CausaDBConfig.from_env()
        assert cfg.telemetry_enabled is True


# ---------------------------------------------------------------------------
# 2. CLI telemetry commands
# ---------------------------------------------------------------------------

class TestTelemetryCLI:
    """Test `causadb telemetry` subcommand."""

    def test_telemetry_status(self, capsys):
        from causadb.cli.main import main
        rc = main(["telemetry", "status"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert "enabled" in payload

    def test_telemetry_off_on(self, capsys):
        from causadb.cli.main import main
        # Turn off
        rc = main(["telemetry", "off"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload.get("status") == "disabled"
        # Turn on
        rc = main(["telemetry", "on"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload.get("status") == "enabled"

    def test_telemetry_export(self, tmp_path, monkeypatch, capsys):
        from causadb._telemetry import reset_counters
        monkeypatch.setattr("causadb._telemetry._get_telemetry_dir", lambda: str(tmp_path))
        monkeypatch.setattr("causadb._telemetry.TELEMETRY_DIR", None)
        import causadb._telemetry as tel
        monkeypatch.setattr(tel, "TELEMETRY_DIR", None)
        tel.TELEMETRY_DIR = None

        reset_counters()

        from causadb.cli.main import main
        rc = main(["telemetry", "export"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert isinstance(payload, list)


# ---------------------------------------------------------------------------
# 3. REST API config endpoints
# ---------------------------------------------------------------------------

class TestTelemetryAPI:
    """Test GET/PUT /api/config for telemetry."""

    def test_telemetry_status_api(self, ledger_and_server):
        """GET /api/config returns telemetry_enabled."""
        _, port, _ = ledger_and_server
        status, data = _get(port, "/api/config")
        assert status == 200
        assert "telemetry_enabled" in data

    def test_telemetry_update_api(self, ledger_and_server):
        """PUT /api/config updates telemetry_enabled."""
        _, port, _ = ledger_and_server
        status, data = _put(port, "/api/config", {"telemetry_enabled": False})
        assert status == 200
        # Verify after update
        status, data = _get(port, "/api/config")
        assert status == 200
        assert data.get("telemetry_enabled") is False


# ---------------------------------------------------------------------------
# 4. Anti-teatro: no silent collection when disabled
# ---------------------------------------------------------------------------

class TestTelemetryAntiTeatro:
    """Verify absolute silence when telemetry is disabled."""

    def test_anti_teatro_no_silent_collect_when_disabled(self, monkeypatch):
        """Article IX: mock is_enabled=False, increment('test'), assert counter still 0."""
        from causadb._telemetry import increment, get_counters, reset_counters
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: False)
        reset_counters()
        increment("sneaky_counter")
        counters = get_counters()
        assert counters.get("sneaky_counter") is None, (
            f"Counter should be None (not collected), got {counters}"
        )

    def test_anti_teatro_multiple_increments_disabled(self, monkeypatch):
        from causadb._telemetry import increment, get_counters, reset_counters
        monkeypatch.setattr("causadb._telemetry.is_enabled", lambda: False)
        reset_counters()
        for _ in range(10):
            increment("noisy")
        counters = get_counters()
        # Could be 0 or None — both mean no collection
        assert not counters.get("noisy"), f"Expected no collection, got {counters}"


# ---------------------------------------------------------------------------
# 5. Init telemetry question
# ---------------------------------------------------------------------------

class TestInitTelemetry:
    """Test init --telemetry flag."""

    def test_init_telemetry_flag(self, tmp_path, monkeypatch, capsys):
        """`causadb init --telemetry-enabled false` skips telemetry."""
        from causadb.cli.main import main
        monkeypatch.chdir(tmp_path)
        workspace = str(tmp_path / "with_telemetry")
        rc = main(["init", workspace, "--telemetry-enabled", "false"])
        captured = capsys.readouterr()
        assert rc == 0, f"exit {rc}: {captured.out}"
        payload = json.loads(captured.out)
        assert payload.get("telemetry_enabled") is False

    def test_init_telemetry_default_on(self, tmp_path, monkeypatch, capsys):
        """`causadb init` defaults telemetry to enabled."""
        from causadb.cli.main import main
        monkeypatch.chdir(tmp_path)
        workspace = str(tmp_path / "default_telemetry")
        rc = main(["init", workspace])
        captured = capsys.readouterr()
        assert rc == 0, f"exit {rc}: {captured.out}"
        payload = json.loads(captured.out)
        assert payload.get("telemetry_enabled") is True


# ---------------------------------------------------------------------------
# Helpers for REST API tests
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_and_server(tmp_path):
    from causadb._init import causadb_init
    from causadb._rest_api import serve_in_thread
    result = causadb_init(str(tmp_path / "ws"))
    ledger = result["ledger_path"]
    server = serve_in_thread(ledger, port=0)
    port = server.server_port
    yield ledger, port, server
    server.shutdown()


def _get(port, path):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def _put(port, path, body):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("PUT", path, json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data
