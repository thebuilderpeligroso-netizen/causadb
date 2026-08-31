"""HarvestSource — puntita Freqtrade (Fase 15.5).

Lee las órdenes ejecutadas por un bot Freqtrade desde su store SQLite
(``tradesv3.sqlite`` — el bot lo crea al hacer trades) y las convierte
en eventos canónicos ``TRADE_EXECUTED``.

Schema real (verificado en Freqtrade 2026.7, ``trade_model.py:1708``,
``record_version=2``):

  - ``trades`` (id INTEGER PRIMARY KEY, exchange, pair, is_open,
    fee_open, fee_open_cost, fee_open_currency, fee_close, ...,
    open_rate, close_rate, realized_profit, close_profit,
    close_profit_abs, stake_amount, amount, open_date, close_date,
    stop_loss, max_rate, exit_reason, strategy, enter_tag, timeframe,
    trading_mode, leverage, is_short, record_version)

Mapeo (una fila ``trades`` → uno o dos ``TRADE_EXECUTED``):

  1. ENTRADA (siempre): ``TRADE_EXECUTED`` con
     - ``symbol = pair``
     - ``side = "buy"`` si ``not is_short`` (long) o ``"sell"`` (short)
     - ``qty = amount``
     - ``price = open_rate``
     - payload extra: exchange, strategy, enter_tag, stake_amount,
       timeframe, trading_mode, leverage, is_short, fee_open, ...

  2. SALIDA (si ``not is_open and close_date is not None``): segundo
     ``TRADE_EXECUTED`` con
     - ``side`` invertido respecto de la entrada (long → ``"sell"``)
     - ``price = close_rate``
     - payload extra: realized_profit, close_profit, close_profit_abs,
       exit_reason, fee_close, max_rate, ...

Spec ``TRADE_EXECUTED`` (tradingview/adapter.py:24-27) requiere
``{symbol, side, qty, price}``. El harvester NO valida required_fields
al escribir (``_event_from_raw`` solo normaliza), pero emitimos los 4
campos SIEMPRE para consistencia futura (Fase 15.9).

Cursor: ``{"max_trade_id": int}`` — barrido secuencial por ``trades.id``
(autoincrement). Solo avanza sobre eventos efectivamente escritos
(atomicidad, Artículo I).

Conexión: ``sqlite3.connect("file:...?mode=ro", uri=True)`` — read-only.
Env override: ``CAUSADB_FREQTRADE_DB_PATH``.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from causadb._event_registry import EventTypeSpec, register_type
from causadb._harvest_source import HarvestSource


# ---------------------------------------------------------------------------
# Registro de event type custom (idempotente — ya registrado por
# tradingview/adapter.py y _harvest_source_mt5.py, pero este módulo debe
# ser autónomo: se importa en daemon/recover sin tocar tradingview).
# Especificación: {symbol, side, qty, price} (spec de tradingview, que es
# la referencia canónica). Ver BIT-CHR.18 TRADE_EXECUTED spec (docs/design_index.md) para la
# normalización de specs competidos (futuro).
# ---------------------------------------------------------------------------

try:
    register_type(
        "TRADE_EXECUTED",
        EventTypeSpec(required_fields={"symbol", "side", "qty", "price"}),
    )
except Exception:
    pass  # ya registrado — idempotente


def _derive_default_db_path() -> str:
    """Store de Freqtrade: env override o ``~/freqtrade/tradesv3.sqlite``."""
    env_path = os.environ.get("CAUSADB_FREQTRADE_DB_PATH")
    if env_path:
        return env_path
    return os.path.join(os.path.expanduser("~"), "freqtrade", "tradesv3.sqlite")


def _normalize_timestamp(ts) -> str:
    """Normaliza un timestamp SQL a ISO 8601 (sustituye espacio por T)."""
    if not ts:
        return ""
    s = str(ts)
    if "T" not in s:
        s = s.replace(" ", "T")
    return s


def _trade_to_raws(row: tuple) -> list[dict]:
    """Mapea UNA fila ``trades`` a 1 o 2 raw dicts ``TRADE_EXECUTED``.

    Campos esperados (orden del SELECT en harvest()):
      id, exchange, pair, is_open, is_short,
      fee_open, fee_open_cost, fee_open_currency,
      fee_close, fee_close_cost, fee_close_currency,
      open_rate, close_rate,
      realized_profit, close_profit, close_profit_abs,
      stake_amount, amount,
      open_date, close_date,
      stop_loss, max_rate, min_rate,
      exit_reason, strategy, enter_tag,
      timeframe, trading_mode, leverage
    """
    (trade_id, exchange, pair, is_open, is_short,
     fee_open, fee_open_cost, fee_open_currency,
     fee_close, fee_close_cost, fee_close_currency,
     open_rate, close_rate,
     realized_profit, close_profit, close_profit_abs,
     stake_amount, amount,
     open_date, close_date,
     stop_loss, max_rate, min_rate,
     exit_reason, strategy, enter_tag,
     timeframe, trading_mode, leverage) = row

    entry_side = "sell" if is_short else "buy"
    exit_side = "buy" if is_short else "sell"
    open_ts = _normalize_timestamp(open_date)
    close_ts = _normalize_timestamp(close_date) if close_date else None

    # -- ENTRADA -------------------------------------------------------
    entry_raw = {
        "type": "TRADE_EXECUTED",
        "timestamp": open_ts,
        "symbol": pair,
        "side": entry_side,
        "qty": amount,
        "price": open_rate,
        "trade_id": trade_id,
        "phase": "entry",
        "exchange": exchange,
        "strategy": strategy,
        "enter_tag": enter_tag,
        "stake_amount": stake_amount,
        "timeframe": timeframe,
        "trading_mode": trading_mode,
        "leverage": leverage,
        "is_short": bool(is_short),
        "fee_open": fee_open,
        "fee_open_cost": fee_open_cost,
        "fee_open_currency": fee_open_currency,
    }
    if stop_loss:
        entry_raw["stop_loss"] = stop_loss

    raws = [entry_raw]

    # -- SALIDA (solo si cerrado) -------------------------------------
    if not is_open and close_date is not None and close_rate is not None:
        exit_raw = {
            "type": "TRADE_EXECUTED",
            "timestamp": close_ts,
            "symbol": pair,
            "side": exit_side,
            "qty": amount,
            "price": close_rate,
            "trade_id": trade_id,
            "phase": "exit",
            "exchange": exchange,
            "strategy": strategy,
            "exit_reason": exit_reason,
            "realized_profit": realized_profit,
            "close_profit": close_profit,
            "close_profit_abs": close_profit_abs,
            "fee_close": fee_close,
            "fee_close_cost": fee_close_cost,
            "fee_close_currency": fee_close_currency,
            "max_rate": max_rate,
            "min_rate": min_rate,
        }
        raws.append(exit_raw)

    return raws


class FreqtradeHarvestSource(HarvestSource):
    """Fuente de harvest para las órdenes de un bot Freqtrade.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        db_path: Ruta al store SQLite de Freqtrade. Default:
            ``CAUSADB_FREQTRADE_DB_PATH`` o ``~/freqtrade/tradesv3.sqlite``
            (override para tests).
    """

    def __init__(self, ledger_path: str, db_path: Optional[str] = None):
        super().__init__(ledger_path)
        self.db_path = db_path or _derive_default_db_path()

    def source_type(self) -> str:
        return "freqtrade"

    def cursor_key(self) -> str:
        return "harvest.freqtrade"

    def detect(self) -> bool:
        return os.path.isfile(self.db_path)

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        cursor = cursor or {}
        max_trade_id = int(cursor.get("max_trade_id", 0))
        raws: list[dict] = []

        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            query = (
                "SELECT t.id, t.exchange, t.pair, t.is_open, t.is_short, "
                "t.fee_open, t.fee_open_cost, t.fee_open_currency, "
                "t.fee_close, t.fee_close_cost, t.fee_close_currency, "
                "t.open_rate, t.close_rate, "
                "t.realized_profit, t.close_profit, t.close_profit_abs, "
                "t.stake_amount, t.amount, "
                "t.open_date, t.close_date, "
                "t.stop_loss, t.max_rate, t.min_rate, "
                "t.exit_reason, t.strategy, t.enter_tag, "
                "t.timeframe, t.trading_mode, t.leverage "
                "FROM trades t "
                "WHERE t.id > ? "
                "ORDER BY t.id"
            )
            rows = con.execute(query, (max_trade_id,)).fetchall()

            for row in rows:
                trade_id = row[0]
                event_raws = _trade_to_raws(row)
                for raw in event_raws:
                    raw["__harvest_rowid"] = trade_id
                    raws.append(raw)
        finally:
            con.close()

        return raws

    def advance_cursor(
        self, cursor: dict | None, harvested_raw_events: list[dict]
    ) -> dict:
        cursor = cursor or {}
        new_max = int(cursor.get("max_trade_id", 0))
        for ev in harvested_raw_events:
            rid = ev.get("__harvest_rowid")
            if rid is not None:
                new_max = max(new_max, int(rid))
        return {"max_trade_id": new_max}
