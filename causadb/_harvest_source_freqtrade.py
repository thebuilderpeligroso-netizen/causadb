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

Fase B-1 (indicadores entry/exit, backward compatible): si
``CAUSADB_FREQTRADE_ANALYSIS_CSV`` apunta al CSV de
``freqtrade backtesting-analysis --analysis-to-csv``, cada
``TRADE_EXECUTED`` se enriquece con ``indicators_entry`` (dict en el
orden de la allowlist) + ``indicators_provenance`` — solo cuando hay
match. Sin sidecar o sin match, las claves están ausentes (nunca None)
y el harvest es idéntico al de hoy. Fail-closed: sidecar ilegible →
warning + sin indicators, nunca aborta.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from causadb._event_registry import EventTypeSpec, register_type
from causadb._harvest_source import HarvestSource

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Fase B-1 — sidecar CSV de backtesting-analysis (puro, ~40 líneas)
# ---------------------------------------------------------------------------

_DEFAULT_INDICATORS = ("rsi", "ema_9", "ema_21", "macd", "bb_lowerband", "bb_upperband")
_MAX_INDICATORS = 6  # cota de peso del ledger
_TIMEFRAME_S = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_DEFAULT_TF_S = 300
_MAX_SIGNAL_ROWS = 50000
_MAX_CSV_BYTES = 20 * 1024 * 1024
_SIGNALS_CACHE: dict = {}  # (path, mtime) -> signals (solo lectura)
_TZ_MILLIS_RE = re.compile(r"(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _parse_indicators_allowlist() -> list[str]:
    raw = os.environ.get("CAUSADB_FREQTRADE_INDICATORS", "")
    inds = [p.strip() for p in raw.split(",") if p.strip()] or list(_DEFAULT_INDICATORS)
    if len(inds) > _MAX_INDICATORS:
        logger.warning("freqtrade indicators: allowlist recortada a %d", _MAX_INDICATORS)
        inds = inds[:_MAX_INDICATORS]
    return inds


def _normalize_signal_timestamp(ts) -> str:
    """_normalize_timestamp + strip de zona horaria y milisegundos."""
    return _TZ_MILLIS_RE.sub("", _normalize_timestamp(ts).strip())


def _timeframe_to_seconds(tf) -> int:
    s = str(tf).strip() if tf is not None else ""
    if s in _TIMEFRAME_S:
        return _TIMEFRAME_S[s]
    logger.warning("freqtrade indicators: timeframe %r desconocido, default %ds", tf, _DEFAULT_TF_S)
    return _DEFAULT_TF_S


def _parse_number(v):
    s = str(v).strip() if v is not None else ""
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return s


def _load_signals_csv(path, allowlist) -> dict:
    """Sidecar → ``{(pair, ts_norm): {ind: valor}}`` (solo lectura).

    Claves de entry salen de ``open_date`` (con columnas ``X (entry)``) y
    las de exit de ``close_date`` (con ``X (exit)``) — nunca cruzadas.
    Fail-closed: cualquier problema → warning + ``{}``.
    """
    if not path or not os.path.isfile(path):
        logger.warning("freqtrade indicators: sidecar no legible: %r", path)
        return {}
    if os.path.getsize(path) > _MAX_CSV_BYTES:
        logger.warning("freqtrade indicators: sidecar >20MB, se ignora")
        return {}
    key = (path, os.path.getmtime(path))
    if key in _SIGNALS_CACHE:
        return _SIGNALS_CACHE[key]
    signals: dict = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            cols = [(c or "").strip() for c in (reader.fieldnames or [])]
            if "pair" not in cols or "open_date" not in cols:
                logger.warning("freqtrade indicators: header sin pair/open_date: %r", reader.fieldnames)
                _SIGNALS_CACHE[key] = {}
                return {}
            ecol = {i: f"{i} (entry)" if f"{i} (entry)" in cols else (i if i in cols else None) for i in allowlist}
            xcol = {i: f"{i} (exit)" if f"{i} (exit)" in cols else (i if i in cols else None) for i in allowlist}
            for n, raw_row in enumerate(reader, 1):
                if n > _MAX_SIGNAL_ROWS:
                    logger.warning("freqtrade indicators: sidecar >50000 filas, se ignora")
                    _SIGNALS_CACHE[key] = {}
                    return {}
                row = {(k or "").strip(): v for k, v in raw_row.items()}
                pair = (row.get("pair") or "").strip()
                if not pair:
                    continue
                for date_col, colmap in (("open_date", ecol), ("close_date", xcol)):
                    if date_col not in cols:
                        continue
                    tnorm = _normalize_signal_timestamp(row.get(date_col))
                    if not tnorm:
                        continue
                    inds = {}
                    for i in allowlist:
                        c = colmap[i]
                        if c is None:
                            continue
                        v = _parse_number(row.get(c))
                        if v is not None:
                            inds[i] = v
                    if inds:
                        signals.setdefault((pair, tnorm), inds)
    except (OSError, UnicodeDecodeError, UnicodeError, csv.Error, ValueError) as e:
        logger.warning("freqtrade indicators: sidecar ilegible (%s), sin indicators", e)
        _SIGNALS_CACHE[key] = {}
        return {}
    _SIGNALS_CACHE[key] = signals
    return signals


def _lookup_indicators(pair, ts, signals, timeframe_s) -> dict | None:
    """Match exacto (pair+ts) y si falla tolerancia ±1×timeframe.

    Múltiples candidatos → menor |delta|; empate → primera fila.
    """
    if not signals:
        return None
    p = (pair or "").strip()
    t = _normalize_signal_timestamp(ts)
    if not p or not t:
        return None
    hit = signals.get((p, t))
    if hit is not None:
        return dict(hit)
    try:
        target = datetime.fromisoformat(t)
    except ValueError:
        return None
    best, best_delta = None, None
    for (sp, st), inds in signals.items():
        if sp != p:
            continue
        try:
            cand = datetime.fromisoformat(st)
        except ValueError:
            continue
        delta = abs((cand - target).total_seconds())
        if delta <= timeframe_s and (best_delta is None or delta < best_delta):
            best, best_delta = inds, delta
    return dict(best) if best is not None else None


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

        # Fase B-1: solo orquesta el enriquecimiento (puro arriba).
        csv_path = os.environ.get("CAUSADB_FREQTRADE_ANALYSIS_CSV", "")
        if csv_path:
            allowlist = _parse_indicators_allowlist()
            signals = _load_signals_csv(csv_path, allowlist)
            if signals:
                for raw in raws:
                    inds = _lookup_indicators(
                        raw.get("symbol"), raw.get("timestamp"), signals,
                        _timeframe_to_seconds(raw.get("timeframe")),
                    )
                    if inds:
                        raw["indicators_entry"] = inds
                        raw["indicators_provenance"] = "backtest-analysis-csv"

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
