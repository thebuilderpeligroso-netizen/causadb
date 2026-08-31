from unittest.mock import patch
"""Tests for --port=0 support (ephemeral port binding).

Artículo III: Test-first. Artículo IX: Anti-teatro.

BIT-CHR.xxx — Port 0 support for ephemeral ports in test isolation.
"""

import json
import glob
import pytest
import time

from causadb._init import causadb_init
from causadb.cli._cmd_serve import cmd_serve, _resolve_port
from causadb._daemon import get_daemon, remove_pidfile, kill_daemon, read_pidfile


class TestPortZeroSupport:
    """Tests for --port=0 ephemeral port binding."""

    def setup_method(self):
        """Ensure no leftover serve processes before each test."""
        platform = get_daemon()
        if platform.is_running("serve"):
            from causadb._daemon import kill_daemon
            kill_daemon("serve", timeout=5.0)
        time.sleep(0.1)

    def test_resolve_port_zero_returns_ephemeral_port(self):
        """_resolve_port(0) returns an ephemeral port and creates port file."""
        actual_port, port_file = _resolve_port(0)
        
        assert 1024 <= actual_port <= 65535
        assert port_file is not None
        assert port_file.endswith("/port.txt")
        
        # Verify port file was created with correct content
        with open(port_file) as f:
            content = f.read().strip()
        assert int(content) == actual_port
        
        # Cleanup
        import os
        os.remove(port_file)
        os.rmdir(os.path.dirname(port_file))

    def test_resolve_port_nonzero_returns_same_port(self):
        """_resolve_port(nonzero) returns same port and no file."""
        actual_port, port_file = _resolve_port(8080)
        
        assert actual_port == 8080
        assert port_file is None

    @patch('causadb.cli._cmd_serve.serve')
    def test_serve_start_accepts_port_zero(self, mock_serve, tmp_path):
        """cmd_serve start accepts --port=0 and binds to ephemeral port."""
        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]

        args = type('Args', (), {
            'action': 'start',
            'ledger': ledger,
            'host': '127.0.0.1',
            'port': 0,
            'daemon': False
        })()

        # Mock serve to return immediately
        mock_serve.return_value = None
        
        exit_code, output = cmd_serve(args)

        assert exit_code == 0, f"cmd_serve failed: {output}"
        data = json.loads(output)
        assert data.get("status") == "stopped"
        mock_serve.assert_called_once()

    def test_serve_start_writes_port_file(self, tmp_path):
        """When port=0, daemon writes actual port to /tmp/causadb-test-<uuid>/port.txt"""
        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]

        args = type('Args', (), {
            'action': 'start',
            'ledger': ledger,
            'host': '127.0.0.1',
            'port': 0,
            'daemon': False
        })()

        with patch('causadb.cli._cmd_serve.serve') as mock_serve:
            mock_serve.return_value = None
            exit_code, output = cmd_serve(args)

        assert exit_code == 0
        # Check that port file was created
        import glob
        port_files = glob.glob("/tmp/causadb-test-*/port.txt")
        assert len(port_files) > 0, "Port file should be created"
        
        with open(port_files[0]) as f:
            port = int(f.read().strip())
        assert 1024 <= port <= 65535, f"Port {port} should be in valid range"

    def test_port_zero_binds_ephemeral(self, tmp_path):
        """Port 0 actually binds to an ephemeral port (not literal 0)."""
        ws = tmp_path / "ws"
        result = causadb_init(str(ws))
        ledger = result["ledger_path"]

        args = type('Args', (), {
            'action': 'start',
            'ledger': ledger,
            'host': '127.0.0.1',
            'port': 0,
            'daemon': False
        })()

        with patch('causadb.cli._cmd_serve.serve') as mock_serve:
            mock_serve.return_value = None
            exit_code, output = cmd_serve(args)

        assert exit_code == 0
        data = json.loads(output)
        assert data.get("status") == "stopped"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
