"""Session-wide fixtures for test isolation.

Provides:
- _isolate_last_workspace: Isolates the last-workspace registry (autouse)
- mock_systemd: Mocks systemd for unit tests (opt-in)
- isolated_systemd_service: Creates isolated systemd service for integration tests
- anti_teatro_fixture: Marks tests as anti-teatro (no mocks)
- Auto-marking based on explicit marks and fixtures
"""

import os
import uuid
import time
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

# =============================================================================
# Pytest markers
# =============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: fast tests, mocked dependencies (< 1s each)")
    config.addinivalue_line("markers", "integration: slow tests, real services/systemd (1-30s each)")
    config.addinivalue_line("markers", "anti_teatro: tests that verify REAL behavior, MUST NOT be mocked")
    config.addinivalue_line("markers", "benchmark: performance benchmarks, run separately with --run-benchmarks")


def pytest_collection_modifyitems(config, items):
    """Auto-mark tests based on explicit marks and fixtures.
    
    Unknown tests default to INTEGRATION (not unit) so the mock never
    applies to tests that weren't explicitly designated as unit.
    """
    for item in items:
        # Explicit marks take precedence
        if item.get_closest_marker("unit") or item.get_closest_marker("integration") or item.get_closest_marker("anti_teatro"):
            continue
        
        # Fixture-based marking
        if "isolated_systemd_service" in item.fixturenames:
            item.add_marker(pytest.mark.integration)
        elif "anti_teatro_fixture" in item.fixturenames:
            item.add_marker(pytest.mark.anti_teatro)
        # Fallback: default to integration (NOT unit) - avoids mock blast radius
        else:
            item.add_marker(pytest.mark.integration)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def _isolate_last_workspace(tmp_path):
    """Point the last-workspace registry at a fresh temp file per test.

    Recording hooks (init, revive, workspace switch, MCP resolution) write
    here instead of the real ``~/.causadb/last_workspace.json``, keeping the
    user registry untouched and tests deterministic (no cross-test leakage).
    """
    os.environ["CAUSADB_LAST_WORKSPACE"] = str(tmp_path / "last_workspace.json")
    yield
    os.environ.pop("CAUSADB_LAST_WORKSPACE", None)


@pytest.fixture
def mock_systemd_for_unit_tests(request, monkeypatch):
    """Mock systemd for unit tests (OPT-IN, not autouse).

    Unit tests that need to avoid real systemd must request this fixture
    explicitly. It is NOT applied automatically to avoid breaking tests
    that legitimately test systemd/daemon behavior.
    """
    from causadb._daemon import get_daemon
    
    # Mock the daemon instance
    mock_daemon = MagicMock()
    mock_daemon.is_running.return_value = False
    mock_daemon.kill.return_value = True
    mock_daemon.daemonize = MagicMock()
    
    def mock_get_daemon():
        return mock_daemon
    
    # Mock get_daemon in all modules that import it
    monkeypatch.setattr("causadb._daemon.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_serve.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_vigilante.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_harvest.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_proxy.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_mcp_proxy.get_daemon", mock_get_daemon)
    monkeypatch.setattr("causadb.cli._cmd_watch.get_daemon", mock_get_daemon)
    
    # Mock systemctl commands in _daemon_service
    def mock_systemctl_cmd(cmd, *args, **kwargs):
        return (True, "mocked")
    
    monkeypatch.setattr("causadb._daemon_service._systemctl_cmd", mock_systemctl_cmd)
    monkeypatch.setattr("causadb._daemon_service.install_service", lambda *a, **k: (True, "/mock/path"))
    monkeypatch.setattr("causadb._daemon_service.start_service", lambda *a, **k: (True, "started"))
    monkeypatch.setattr("causadb._daemon_service.stop_service", lambda *a, **k: (True, "stopped"))


@pytest.fixture
def isolated_systemd_service(tmp_path, request):
    """Isolated systemd service fixture for integration tests.
    
    Creates a unique systemd service instance with:
    - Unique service name: causadb-test-<uuid>
    - Isolated ledger directory in tmp_path
    - Automatic cleanup via request.addfinalizer
    
    Yields dict with: uuid, ledger_dir, ledger_path, port
    """
    from causadb._init import causadb_init
    from causadb._daemon import kill_daemon
    
    uuid_str = uuid.uuid4().hex[:8]
    
    ledger_dir = tmp_path / f"causadb-test-{uuid_str}"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize ledger
    from causadb._init import causadb_init
    result = causadb_init(str(ledger_dir))
    ledger_path = result["ledger_path"]
    
    # Create systemd unit file (template service)
    unit_file = os.path.expanduser(f"~/.config/systemd/user/causadb-test@{uuid_str}.service")
    os.makedirs(os.path.dirname(unit_file), exist_ok=True)
    unit_content = f"""[Unit]
Description=CausaDB Test Instance {uuid_str}
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m causadb.cli.main serve start --ledger {ledger_path} --port 0
Restart=no

[Install]
WantedBy=default.target
"""
    with open(unit_file, "w") as f:
        f.write(unit_content)
    
    # Install and start
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "start", f"causadb-test@{uuid_str}"], check=True)
    
    # Wait for service to start
    time.sleep(1)
    
    # Read port from port file written by _resolve_port
    port = None
    import glob
    for _ in range(10):
        port_files = glob.glob("/tmp/causadb-test-*/port.txt")
        if port_files:
            with open(port_files[-1]) as f:
                port = int(f.read().strip())
            break
        time.sleep(0.1)
    
    def cleanup():
        import subprocess
        subprocess.run(["systemctl", "--user", "stop", f"causadb-test@{uuid_str}"], 
                      capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", f"causadb-test@{uuid_str}"], 
                      capture_output=True)
        if os.path.exists(unit_file):
            os.remove(unit_file)
        # Cleanup any leftover processes
        from causadb._daemon import kill_daemon
        kill_daemon("serve", timeout=5.0)
    
    # Register cleanup
    request.addfinalizer(cleanup)
    
    yield {
        "uuid": uuid_str,
        "service_name": f"causadb-test@{uuid_str}",
        "ledger_dir": str(ledger_dir),
        "ledger_path": ledger_path,
        "port": port,
    }


@pytest.fixture
def anti_teatro_fixture():
    """Fixture to mark test as anti-teatro (must not mock).
    
    Tests using this fixture will be marked with @pytest.mark.anti_teatro
    and will NOT have systemd mocked.
    """
    return {"anti_teatro": True}


# =============================================================================
# Utility fixtures
# =============================================================================

@pytest.fixture
def temp_ledger(tmp_path):
    """Create a temporary ledger for testing."""
    from causadb._init import causadb_init
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    return result["ledger_path"]



@pytest.fixture
def watch_cleanup():
    """Cleanup fixture for watch integration tests.
    
    Kills any leftover CausaDB daemon processes that the watch
    start/stop tests may leave behind (vigilante, proxy-server,
    harvest, mcp-proxy). Must be requested explicitly by watch
    integration tests.
    """
    yield
    import subprocess
    for pattern in ["causadb.cli.main proxy-server", "causadb.cli.main harvest",
                    "causadb.cli.main vigilante", "causadb.cli.main mcp-proxy"]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=2,
            )
            pids = result.stdout.strip().splitlines() if result.stdout.strip() else []
            for pid in pids:
                try:
                    os.kill(int(pid), 9)
                except (ProcessLookupError, ValueError):
                    pass
        except Exception:
            pass
