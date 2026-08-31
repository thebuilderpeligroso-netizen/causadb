"""Tests for Jupyter adapter (G.3).

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import json
import os

import pytest

from causadb.adapters.jupyter.adapter import (
    log_cell_execution,
    log_dataframe_load,
)
from causadb._ledger_writer import LedgerWriter
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_path(tmp_path):
    """Crea un path absoluto a un ledger temporal."""
    return str(tmp_path / "ledger.log")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJupyterAdapter:
    def test_jupyter_logs_cell_execution(self, ledger_path):
        """log_cell_execution escribe un evento COMMAND_RUN en el ledger."""
        result = log_cell_execution(
            "print('hello')",
            "hello",
            ledger_path=ledger_path,
        )

        # Verificar resultado
        assert "event_id" in result
        assert "hash" in result
        assert "timestamp" in result

        # Leer el ledger y verificar el contenido
        with open(ledger_path) as f:
            entry = json.loads(f.readline())

        assert entry["event"]["event_type"] == EventType.COMMAND_RUN.value
        assert entry["event"]["payload"]["cell"] == "print('hello')"
        assert entry["event"]["payload"]["output_truncated"] is False
        assert entry["event"]["source"] == "jupyter"
        assert entry["event"]["ctx_id"] == "jupyter"

    def test_jupyter_logs_cell_execution_truncates(self, ledger_path):
        """Código de celda se trunca a 500 caracteres."""
        long_code = "x = 1\n" * 200  # ~1200 chars
        result = log_cell_execution(
            long_code,
            "ok",
            ledger_path=ledger_path,
        )

        with open(ledger_path) as f:
            entry = json.loads(f.readline())

        cell = entry["event"]["payload"]["cell"]
        assert len(cell) <= 500
        assert cell.endswith("...") or len(cell) == 500

    def test_jupyter_logs_dataframe_load(self, ledger_path):
        """log_dataframe_load escribe un evento DATA_LOADED en el ledger."""
        result = log_dataframe_load(
            "data.csv",
            1500,
            10,
            ledger_path=ledger_path,
        )

        # Verificar resultado
        assert "event_id" in result
        assert "hash" in result
        assert "timestamp" in result

        # Leer el ledger y verificar el contenido
        with open(ledger_path) as f:
            entry = json.loads(f.readline())

        assert entry["event"]["event_type"] == "DATA_LOADED"
        assert entry["event"]["payload"]["source"] == "data.csv"
        assert entry["event"]["payload"]["rows"] == 1500
        assert entry["event"]["payload"]["columns"] == 10
        assert entry["event"]["source"] == "jupyter"

    def test_jupyter_invalid_ledger_raises(self):
        """Anti-teatro: ledger path inválido debe fallar."""
        with pytest.raises((ValueError, FileNotFoundError, OSError)):
            log_cell_execution(
                "print('x')",
                "x",
                ledger_path="/no/existe/ledger.log",
            )

    def test_jupyter_output_truncated_flag(self, ledger_path):
        """output_truncated es True cuando output > 100 chars."""
        long_output = "a" * 200

        log_cell_execution(
            "print('long')",
            long_output,
            ledger_path=ledger_path,
        )

        with open(ledger_path) as f:
            entry = json.loads(f.readline())

        assert entry["event"]["payload"]["output_truncated"] is True
