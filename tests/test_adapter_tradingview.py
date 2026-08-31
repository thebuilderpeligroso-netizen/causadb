"""
Tests para el adapter TradingView (G.4).

Verifica que:
  1. ``log_trade_order`` escribe un evento TRADE_EXECUTED en el ledger.
  2. ``log_risk_check`` escribe un evento RISK_CHECKED en el ledger.
"""

import json
import pytest
from causadb.adapters.tradingview.adapter import (
    log_trade_order,
    log_risk_check,
)


class TestTradingViewAdapter:
    """Test funcional del adapter TradingView."""

    def test_tradingview_logs_order(self, tmp_path):
        """log_trade_order() escribe un evento TRADE_EXECUTED en el ledger."""
        ledger_path = str(tmp_path / "ledger.log")

        result = log_trade_order("BTCUSD", "buy", 0.1, 50000,
                                 ledger_path=ledger_path)

        # Verificar que la función devuelve metadatos del evento
        assert "event_id" in result
        assert "hash" in result
        assert "timestamp" in result

        # Leer el ledger y verificar el contenido
        with open(ledger_path, "r") as f:
            lines = f.readlines()

        # Debe haber exactamente 1 linea (el evento TRADE_EXECUTED)
        assert len(lines) == 1, (
            f"Esperaba 1 linea en el ledger, encontradas {len(lines)}"
        )

        entry = json.loads(lines[0])
        ev = entry["event"]

        assert ev["event_type"] == "TRADE_EXECUTED"
        assert ev["payload"]["symbol"] == "BTCUSD"
        assert ev["payload"]["side"] == "buy"
        assert ev["payload"]["qty"] == 0.1
        assert ev["payload"]["price"] == 50000
        assert ev["ctx_id"] == "tradingview"
        assert ev["source"] == "tradingview:bot"
        assert entry["prev_hash"] == "GENESIS"

    def test_tradingview_logs_risk_check(self, tmp_path):
        """log_risk_check() escribe un evento RISK_CHECKED en el ledger."""
        ledger_path = str(tmp_path / "ledger_risk.log")

        result = log_risk_check("BTCUSD", True, 2.0,
                                ledger_path=ledger_path)

        # Verificar que la función devuelve metadatos del evento
        assert "event_id" in result
        assert "hash" in result
        assert "timestamp" in result

        # Leer el ledger y verificar el contenido
        with open(ledger_path, "r") as f:
            lines = f.readlines()

        # Debe haber exactamente 1 linea
        assert len(lines) == 1, (
            f"Esperaba 1 linea en el ledger, encontradas {len(lines)}"
        )

        entry = json.loads(lines[0])
        ev = entry["event"]

        assert ev["event_type"] == "RISK_CHECKED"
        assert ev["payload"]["symbol"] == "BTCUSD"
        assert ev["payload"]["risk_ok"] is True
        assert ev["payload"]["max_risk_pct"] == 2.0
        assert ev["ctx_id"] == "tradingview"
        assert ev["source"] == "tradingview:bot"
        assert entry["prev_hash"] == "GENESIS"
