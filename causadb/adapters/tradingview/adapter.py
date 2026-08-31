"""
CausaDB → TradingView Bot Adapter (G.4).

Adapter para un bot de trading que recibe señales de TradingView via webhook
y loggea órdenes y risk checks a CausaDB.

Eventos:
  - ``TRADE_EXECUTED``: Se ejecutó una orden de trading.
  - ``RISK_CHECKED``: Se realizó un check de riesgo.

Delega en ``causadb.adapters.template.log_event()`` (Artículo II —
thin wrapper). No reimplementa lógica.
"""

from typing import Any, Dict, Optional

from causadb.adapters.template import log_event
from causadb._event_registry import register_type, EventTypeSpec

# ---------------------------------------------------------------------------
# Registro de event types custom (G.4)
# ---------------------------------------------------------------------------

register_type(
    "TRADE_EXECUTED",
    EventTypeSpec(required_fields={"symbol", "side", "qty", "price"}),
)
register_type(
    "RISK_CHECKED",
    EventTypeSpec(required_fields={"symbol", "risk_ok", "max_risk_pct"}),
)

# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def log_trade_order(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a trade order execution to CausaDB.

    Registra un evento ``TRADE_EXECUTED`` con los detalles de la orden.

    Args:
        symbol: Símbolo del activo (e.g. ``"BTCUSD"``).
        side: Lado de la orden (``"buy"`` o ``"sell"``).
        qty: Cantidad ejecutada.
        price: Precio de ejecución.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger o la validación.
    """
    return log_event(
        event_type="TRADE_EXECUTED",
        payload={
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
        },
        ctx_id="tradingview",
        source="tradingview:bot",
        ledger_path=ledger_path,
    )


def log_risk_check(
    symbol: str,
    risk_ok: bool,
    max_risk_pct: float,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Log a risk check result to CausaDB.

    Registra un evento ``RISK_CHECKED`` con el resultado del análisis
    de riesgo.

    Args:
        symbol: Símbolo del activo evaluado (e.g. ``"BTCUSD"``).
        risk_ok: ``True`` si la operación pasa los controles de riesgo,
            ``False`` en caso contrario.
        max_risk_pct: Porcentaje máximo de riesgo permitido.
        ledger_path: Ruta absoluta al archivo ledger. Si se omite, se lee
            de la variable de entorno ``CAUSADB_LEDGER_PATH``.

    Returns:
        Diccionario con ``event_id``, ``hash`` y ``timestamp`` del evento
        registrado.

    Raises:
        ValueError: Si falla la resolución del ledger o la validación.
    """
    return log_event(
        event_type="RISK_CHECKED",
        payload={
            "symbol": symbol,
            "risk_ok": risk_ok,
            "max_risk_pct": max_risk_pct,
        },
        ctx_id="tradingview",
        source="tradingview:bot",
        ledger_path=ledger_path,
    )
