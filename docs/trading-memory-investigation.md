# Trading como memoria verificable — Investigación (sedimentada, sin código)

Fecha: 2026-09-18. Estado: investigación cerrada, nada implementado todavía.

## Decisión de dirección

CausaDB NO compite con bots ni memorias de trading. Es la capa de abajo que les da historial verificable para beber (consulta/export), sin tocar su lógica. Frase: *"Your trading bot can remember its trades. CausaDB lets you reconstruct why they happened."*

## Parte 1 — Qué trae hoy Freqtrade y qué falta

La tabla `trades` trae lo operativo + estrategia, etiqueta de entrada y temporalidad (`_harvest_source_freqtrade.py:10-15`, SELECT `:209-223`). La entrada guarda extras (`:124-145`), la salida (`:152-173`). Falta el POR QUÉ: versión de estrategia, hash de config, indicadores al momento. Fixture real: `tests/fixtures/freqtrade_fixture.sqlite`.

## Parte 2 — Dónde vive la config

El harvester solo conoce la DB (`CAUSADB_FREQTRADE_DB_PATH` o `~/freqtrade/tradesv3.sqlite`, `:75-80`). `strategy_path`/config: cero menciones — solo entra por env, no rastreable desde la DB.

## Parte 3 — Velas e indicadores

No están en `tradesv3.sqlite` ni en el repo (datos externos del exchange). Costo actual cero; costo futuro = abrir archivos par por par + alinear timestamps (caro, fuera del harvester SQLite read-only actual).

## Parte 4 — MT5 y conflicto C-08

MT5 lee `.LOG` por regex y registra `{order,symbol,side}` (`_harvest_source_mt5.py:36-62,152-191`); TradingView exige `{symbol,side,qty,price}` (`adapters/tradingview/adapter.py:24-27`). Dos dialectos del mismo `TRADE_EXECUTED`. Freqtrade ya emite los 4 (compatibilidad futura, plan en `docs/design_index.md:33`).

## Parte 5 — Qué necesitarían los de afuera

TradingAgents: decisión+retorno+reflexión (reflexión hoy no existe). Freqtrade backtest-analysis: trades+señales+indicadores (tenemos 2 de 3). Interfaz lista sin modificarlos: `causadb_query` puntual, REST `POST /api/query`, `POST /api/export` json/csv, webhook de entrada.

## Parte 6 — Adaptador genérico

Ficha mínima = spec TradingView `{symbol,side,qty,price}` + `timestamp` + `source`. Freqtrade aporta lo rico; MT5 `{order,log_file,log_line}` con qty/price vacíos; webhookCSV futuro crudo. Regla: cada fuente completa lo que sabe, nunca inventa.

## Prueba barata (2026-09-18, con fixture real, sin tocar código) ✅

Cosechados 3 eventos de `tests/fixtures/freqtrade_fixture.sqlite` con estrategia, etiqueta de entrada y temporalidad por operación. Veredicto por consumidor:

- TradingAgents (fecha/ticker/decisión/retorno/reflexión): tenemos 4 de 5; falta reflexión (la pone el agente externo, por diseño).
- Freqtrade backtest-analysis (trades+enter/exit tags+indicadores): tenemos trades y tags; faltan indicadores al momento (fase B).

Conclusión: el bebedero sirve hoy para decisiones+resultados; indicadores es lo único que pediría código nuevo.
