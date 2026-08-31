"""HarvestSource — MetaTrader 5 (J.4).

Lee archivos ``.LOG`` del directorio de logs de MT5 (por defecto
``~/.mt5/logs/`` o path configurable) y produce eventos
``TRADE_EXECUTED`` por cada línea de orden encontrada.

Formato de línea esperado (MT5 terminal log):

    YYYY.MM.DD HH:MM:SS || message

El mensaje se parsea para extraer ``order``, ``symbol`` y ``side``
(``"buy"`` / ``"sell"``). Las líneas que no matchean el patrón de
orden se ignoran (degradación suave).

Cursor:
    ``{"file": "last_file.log", "line": N}`` — resume desde la línea
    ``N`` del archivo ``last_file.log``. Si el archivo cambió, se
    recomienza desde la línea 0 del nuevo archivo.

Sin dependencias externas — solo stdlib (``os``, ``re``, ``glob``).
"""

import os
import re
import glob

from causadb._harvest_source import HarvestSource
from causadb._event_registry import register_type, EventTypeSpec

# ---------------------------------------------------------------------------
# Registro de event type custom (idempotente — ya registrado por adapter
# tradingview, pero este módulo debe ser autónomo).
# ---------------------------------------------------------------------------

try:
    register_type(
        "TRADE_EXECUTED",
        EventTypeSpec(required_fields={"order", "symbol", "side"}),
    )
except Exception:
    pass  # ya registrado — idempotente

# ---------------------------------------------------------------------------
# Patrones de parseo
# ---------------------------------------------------------------------------

# Línea de log MT5: "2024.07.28 15:30:00 || mensaje..."
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|\|\s*(?P<msg>.*)$"
)

# Order: "order #12345" o "order 12345" o "#12345"
_ORDER_RE = re.compile(r"order\s*#?\s*(?P<order>\d+)", re.IGNORECASE)

# Symbol: típicamente entre espacios o tras "symbol" — heurística:
# matchea tokens ALLCAPS de 3-12 chars que parecen símbolos de trading
# (ej: "EURUSD", "BTCUSD", "XAUUSD"). Se evita matchear palabras comunes.
_SYMBOL_RE = re.compile(r"\b(?P<symbol>[A-Z]{3,12}(?:USD|EUR|JPY|GBP|CHF|AUD|CAD|NZD))\b")

# Side: "buy" / "sell" como palabras enteras (case-insensitive)
_BUY_RE = re.compile(r"\bbuy\b", re.IGNORECASE)
_SELL_RE = re.compile(r"\bsell\b", re.IGNORECASE)


class MT5HarvestSource(HarvestSource):
    """Fuente de harvest para logs de MetaTrader 5.

    Args:
        ledger_path: Ruta absoluta al ledger (requerido por la clase base).
        source_path: (opcional) Directorio de logs MT5. Por defecto
            ``~/.mt5/logs/``. Si no existe, ``detect()`` retorna False.
    """

    def __init__(self, ledger_path: str, source_path: str | None = None):
        super().__init__(ledger_path)
        self.source_path = source_path or os.path.expanduser("~/.mt5/logs/")

    def source_type(self) -> str:
        return "mt5"

    def cursor_key(self) -> str:
        return "mt5_logs"

    def detect(self) -> bool:
        """True si el directorio existe y contiene al menos un .LOG."""
        if not os.path.isdir(self.source_path):
            return False
        logs = glob.glob(os.path.join(self.source_path, "*.LOG"))
        return len(logs) > 0

    def harvest(self, cursor: dict | None = None) -> list[dict]:
        """Cosecha líneas de orden de los archivos .LOG.

        Resume desde el cursor ``{"file": ..., "line": N}``. Si el
        archivo del cursor ya no existe o cambió, recomienza desde el
        primer archivo .LOG ordenado alfabéticamente.
        """
        if not self.detect():
            return []

        cursor = cursor or {}
        last_file = cursor.get("file")
        last_line = cursor.get("line", 0)

        logs = sorted(glob.glob(os.path.join(self.source_path, "*.LOG")))

        # Determinar desde qué archivo empezar
        start_index = 0
        if last_file:
            last_path = os.path.join(self.source_path, last_file)
            if last_path in logs:
                start_index = logs.index(last_path)

        events: list[dict] = []
        for i in range(start_index, len(logs)):
            log_path = logs[i]
            # Si es el archivo del cursor, saltamos las líneas ya procesadas
            start_line = last_line if (i == start_index and last_file) else 0
            events.extend(self._parse_log(log_path, start_line))

        return events

    def advance_cursor(self, cursor: dict | None, harvested_raw_events: list[dict]) -> dict:
        cursor = dict(cursor) if cursor else {}
        last_file = cursor.get("file")
        last_line = cursor.get("line", 0)

        if harvested_raw_events:
            last_ev = harvested_raw_events[-1]
            last_file = last_ev.get("log_file", last_file)
            last_line = last_ev.get("log_line", 0) + 1

        return {"file": last_file, "line": last_line}

    # -- Internal -----------------------------------------------------------

    def _parse_log(self, log_path: str, start_line: int) -> list[dict]:
        """Parsea un archivo .LOG desde la línea ``start_line``."""
        events: list[dict] = []
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line_no, raw_line in enumerate(f):
                    if line_no < start_line:
                        continue
                    event = self._parse_line(raw_line, log_path, line_no)
                    if event is not None:
                        events.append(event)
        except OSError:
            return []
        return events

    def _parse_line(self, raw_line: str, log_path: str, line_no: int) -> dict | None:
        """Parsea una línea individual. Retorna un raw dict o None."""
        line = raw_line.rstrip("\n\r")
        m = _LOG_LINE_RE.match(line)
        if m is None:
            return None

        ts_raw = m.group("ts").strip()
        msg = m.group("msg").strip()

        # Extraer order — si no hay order, no es un TRADE_EXECUTED
        order_match = _ORDER_RE.search(msg)
        if order_match is None:
            return None
        order = order_match.group("order")

        # Extraer symbol
        symbol_match = _SYMBOL_RE.search(msg)
        symbol = symbol_match.group("symbol") if symbol_match else ""

        # Extraer side
        if _SELL_RE.search(msg):
            side = "sell"
        elif _BUY_RE.search(msg):
            side = "buy"
        else:
            side = "unknown"

        # Normalizar timestamp "YYYY.MM.DD HH:MM:SS" → ISO 8601
        timestamp = ts_raw.replace(".", "-") + "Z"
        # "2024-07-28 15:30:00Z" — el harvester lo re-normalizará

        return {
            "type": "TRADE_EXECUTED",
            "timestamp": timestamp,
            "order": order,
            "symbol": symbol,
            "side": side,
            "log_file": os.path.basename(log_path),
            "log_line": line_no,
        }
