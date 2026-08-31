# TradingView Bot Adapter — G.4

## ¿Qué hace?

Adapter para un **bot de trading** que recibe señales de **TradingView** via
webhook y loggea órdenes y risk checks a **CausaDB**.

### Eventos que registra

| Evento | Payload | Cuándo |
|--------|---------|--------|
| `TRADE_EXECUTED` | `{"symbol", "side", "qty", "price"}` | Cuando el bot ejecuta una orden de compra/venta |
| `RISK_CHECKED` | `{"symbol", "risk_ok", "max_risk_pct"}` | Cuando el bot evalúa el riesgo antes de operar |

## Funciones públicas

### `log_trade_order(symbol, side, qty, price, ledger_path=None)`

Registra una orden ejecutada.

- **symbol** (`str`): Símbolo del activo (e.g. `"BTCUSD"`).
- **side** (`str`): `"buy"` o `"sell"`.
- **qty** (`float`): Cantidad ejecutada.
- **price** (`float`): Precio de ejecución.
- **ledger_path** (`str | None`): Ruta al ledger. Si se omite, se lee de
  `CAUSADB_LEDGER_PATH`.
- **Returns**: `dict` con `event_id`, `hash`, `timestamp`.

### `log_risk_check(symbol, risk_ok, max_risk_pct, ledger_path=None)`

Registra un control de riesgo.

- **symbol** (`str`): Símbolo evaluado.
- **risk_ok** (`bool`): `True` si pasa, `False` si no.
- **max_risk_pct** (`float`): % máximo de riesgo permitido.
- **ledger_path** (`str | None`): Ruta al ledger.
- **Returns**: `dict` con `event_id`, `hash`, `timestamp`.

## Uso típico

```python
from causadb.adapters.tradingview.adapter import log_trade_order, log_risk_check

# Configurar ruta del ledger
LEDGER = "/home/bot/data/causadb/ledger.log"

# Al recibir un webhook de TradingView:
def on_tradingview_signal(symbol, side, qty, price):
    # 1. Evaluar riesgo
    risk = log_risk_check(symbol, risk_ok=True, max_risk_pct=2.0,
                          ledger_path=LEDGER)

    # 2. Ejecutar orden (simulado)
    order = log_trade_order(symbol, side, qty, price,
                            ledger_path=LEDGER)

    print(f"Orden ejecutada: {order['event_id']}")
```

## Configuración

El ledger path puede configurarse de dos formas:

1. **Explícito**: pasando `ledger_path=` en cada llamada.
2. **Variable de entorno**: seteando `CAUSADB_LEDGER_PATH`.

```bash
export CAUSADB_LEDGER_PATH="/home/bot/data/causadb/ledger.log"
```

## Tests

```bash
cd causadb && source .venv/bin/activate
python -m pytest tests/test_adapter_tradingview.py -v
```

## Dependencias

- Solo stdlib + clases del núcleo de CausaDB (`LedgerWriter`, `EventType`, etc.).
- No requiere dependencias externas.
