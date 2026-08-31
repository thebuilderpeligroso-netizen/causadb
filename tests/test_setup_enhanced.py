"""Tests Fase 6 — Mejoras en one-command installation (ver Chronicle; docs/design_index.md)."""

from causadb.cli._cmd_setup import cmd_setup
import json
import os
import tempfile
from unittest.mock import patch, MagicMock, call

# Nota: estos tests usan mocking intensivo porque cmd_setup delega
# a subcomandos que dependen de entorno real (shell hooks, git hooks,
# daemon watch). Los tests unitarios verifican orquestación; los
# tests E2E de los subcomandos individuales están en sus respectivos
# test files. Artículo IX: el mocking es explícito y está documentado.


def test_setup_default_telemetry_enabled():
    """Verifica que telemetry se habilita por defecto en setup normal.

    Anti-teatro: verify that 'set_enabled' was called with True and
    that the output structure is well-formed.
    """
    with patch('causadb.cli._cmd_setup.WorkspaceManager') as mock_wm, \
         patch('causadb._shell_hook.install') as mock_shell, \
         patch('causadb._git_hook.install_post_commit_hook') as mock_git, \
         patch('causadb.cli._cmd_watch.cmd_watch') as mock_watch, \
         patch('causadb._telemetry.set_enabled') as mock_telemetry:

        mock_wm_instance = MagicMock()
        mock_wm.discover.return_value = None
        mock_wm.init.return_value = {"config_path": "/fake/ledger"}
        mock_wm.load.return_value = MagicMock()
        mock_wm_instance.ledger_path = "/fake/ledger"
        mock_wm.return_value = mock_wm_instance
        mock_shell.return_value = True
        mock_git.return_value = True
        mock_watch.return_value = (0, '{"status": "ok"}')
        mock_telemetry.return_value = None

        args = type('Args', (), {
            'project_dir': None,
            'no_hook': False,
            'no_git': False,
            'no_watch': False,
            'integrations': None,
            'no_daemon': True
        })()

        exit_code, output = cmd_setup(args)
        assert exit_code == 0

        result = json.loads(output)
        assert result["steps"]["init"]["status"] == "ok"
        assert result["steps"]["shell_hook"]["status"] == "ok"
        assert result["steps"]["git_hook"]["status"] == "ok"
        assert result["steps"]["watch"]["status"] == "ok"
        assert result["steps"]["telemetry"]["status"] == "ok"
        assert result["steps"]["telemetry"]["detail"] == "enabled"
        mock_telemetry.assert_called_once_with(True)


def test_setup_init_failure_still_marks_watch_as_ok():
    """Verifica degradación suave: si init falla, los pasos siguientes
    igual se ejecutan (no corta cadena)."""
    with patch('causadb.cli._cmd_setup.WorkspaceManager') as mock_wm, \
         patch('causadb._shell_hook.install') as mock_shell, \
         patch('causadb._git_hook.install_post_commit_hook') as mock_git, \
         patch('causadb.cli._cmd_watch.cmd_watch') as mock_watch, \
         patch('causadb._telemetry.set_enabled') as mock_telemetry:

        mock_wm_instance = MagicMock()
        mock_wm.discover.return_value = None
        mock_wm.init.side_effect = ValueError("no project")
        mock_wm.load.return_value = MagicMock()
        mock_wm_instance.ledger_path = "/fake/ledger"
        mock_wm.return_value = mock_wm_instance
        mock_shell.return_value = True
        mock_git.return_value = True
        mock_watch.return_value = (0, '{"status": "ok"}')
        mock_telemetry.return_value = None

        args = type('Args', (), {
            'project_dir': "/test/project",
            'no_hook': False,
            'no_git': False,
            'no_watch': False,
            'integrations': None,
            'no_daemon': True
        })()

        exit_code, output = cmd_setup(args)
        assert exit_code == 0

        result = json.loads(output)
        assert result["steps"]["init"]["status"] == "error"
        assert result["steps"]["shell_hook"]["status"] == "ok"
        assert result["steps"]["watch"]["status"] == "ok"
        mock_telemetry.assert_called_once_with(True)


def test_setup_integrations_all_group():
    """Verifica grupo 'all' de integraciones."""
    with patch('causadb.cli._cmd_setup.WorkspaceManager') as mock_wm, \
         patch('causadb._shell_hook.install'), \
         patch('causadb._git_hook.install_post_commit_hook'), \
         patch('causadb.cli._cmd_watch.cmd_watch') as mock_watch, \
         patch('causadb._telemetry.set_enabled'), \
         patch('causadb.cli._cmd_config.cmd_config') as mock_config:

        mock_wm_instance = MagicMock()
        mock_wm.discover.return_value = None
        mock_wm.init.return_value = {"config_path": "/fake/ledger"}
        mock_wm.load.return_value = MagicMock()
        mock_wm_instance.ledger_path = "/fake/ledger"
        mock_wm.return_value = mock_wm_instance
        mock_watch.return_value = (0, '{"status": "ok"}')
        mock_config.return_value = (0, '{"status": "ok"}')

        args = type('Args', (), {
            'project_dir': "/test/project",
            'no_hook': False,
            'no_git': False,
            'no_watch': False,
            'integrations': "all",
            'no_daemon': True
        })()

        exit_code, output = cmd_setup(args)
        assert exit_code == 0

        expected_tools = {"opencode", "claude-code", "cursor", "windsurf", "gemini-cli", "aider"}
        called_tools = {call.args[0].tool for call in mock_config.call_args_list}
        assert called_tools == expected_tools, f"Expected tools {expected_tools}, got {called_tools}"


def test_setup_integrations_custom_list():
    """Verifica lista personalizada de integraciones."""
    with patch('causadb.cli._cmd_setup.WorkspaceManager') as mock_wm, \
         patch('causadb._shell_hook.install'), \
         patch('causadb._git_hook.install_post_commit_hook'), \
         patch('causadb.cli._cmd_watch.cmd_watch') as mock_watch, \
         patch('causadb._telemetry.set_enabled'), \
         patch('causadb.cli._cmd_config.cmd_config') as mock_config:

        mock_wm_instance = MagicMock()
        mock_wm.discover.return_value = None
        mock_wm.init.return_value = {"config_path": "/fake/ledger"}
        mock_wm.load.return_value = MagicMock()
        mock_wm_instance.ledger_path = "/fake/ledger"
        mock_wm.return_value = mock_wm_instance
        mock_watch.return_value = (0, '{"status": "ok"}')
        mock_config.return_value = (0, '{"status": "ok"}')

        args = type('Args', (), {
            'project_dir': "/test/project",
            'no_hook': False,
            'no_git': False,
            'no_watch': False,
            'integrations': "opencode,claude-code",
            'no_daemon': True
        })()

        exit_code, output = cmd_setup(args)
        assert exit_code == 0

        called_tools = {call.args[0].tool for call in mock_config.call_args_list}
        assert called_tools == {"opencode", "claude-code"}