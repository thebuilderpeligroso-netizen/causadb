"""Tests Fase 15.5 — Puntita Freqtrade (BIT-CHR.18; docs/design_index.md).

Artículo III (test-first), Artículo VI (replay-determinismo), Artículo IX
(fixture REAL — SQLite con schema trades record_version=2 extraído del
patrón de Freqtrade 2026.7 ``trade_model.py``).

Cobertura:
  1. detect() True con fixture / False sin el DB
  2. harvest de la fixture → eventos esperados
     - Trade 1 (abierto, long): 1 TRADE_EXECUTED (entry buy)
     - Trade 2 (cerrado, long): 2 TRADE_EXECUTED (entry buy + exit sell)
     - Total: 3 TRADE_EXECUTED, todos con {symbol, side, qty, price}
  3. dos corridas → segunda devuelve 0 eventos (sin duplicados)
  4. anti-teatro: cursor no avanza si el ledger write falla
  5. replay-determinismo (Artículo VI): mismo harvest → mismo state
  6. anti-teatro: la lectura es read-only (la fixture no se modifica)
"""

import json
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_freqtrade import FreqtradeHarvestSource
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_FILE = "freqtrade_fixture.sqlite"


def _install_fixture(tmp_path):
    """Copia la fixture SQLite a un path temporal."""
    dst = tmp_path / "tradesv3.sqlite"
    shutil.copy(
        os.path.join(FIXTURE_DIR, FIXTURE_FILE),
        dst,
    )
    return str(dst)


def _make_source(tmp_path, ledger_path=None, db_path=None):
    if db_path is None:
        db_path = _install_fixture(tmp_path)
    return FreqtradeHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_fixture(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "freqtrade"
    assert source.cursor_key() == "harvest.freqtrade"


def test_detect_false_without_db(tmp_path):
    source = FreqtradeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=str(tmp_path / "no-existe.sqlite"),
    )
    assert source.detect() is False


# ---------------------------------------------------------------------------
# 2. harvest de la fixture → eventos esperados
# ---------------------------------------------------------------------------

def test_harvest_fixture_maps_to_expected_events(tmp_path):
    """Fixture:
    - Trade 1 (id=1): BTC/USDT, is_open=1 → 1 TRADE_EXECUTED (entry buy)
    - Trade 2 (id=2): ETH/USDT, is_open=0, close_rate=3230.75 →
      2 TRADE_EXECUTED (entry buy + exit sell)
    Total esperado: 3 TRADE_EXECUTED."""
    source = _make_source(tmp_path)
    raws = source.harvest(None)

    types = [r["type"] for r in raws]
    assert types == [
        "TRADE_EXECUTED",
        "TRADE_EXECUTED",
        "TRADE_EXECUTED",
    ], f"Esperaba 3 TRADE_EXECUTED, obtuve {types}"
    assert len(raws) == 3

    # Cada evento debe tener los 4 campos del spec tradingview
    for r in raws:
        assert "symbol" in r and r["symbol"] is not None
        assert "side" in r and r["side"] in ("buy", "sell")
        assert "qty" in r and r["qty"] is not None
        assert "price" in r and r["price"] is not None

    # Entry 1 (trade 1, abierto, long → buy)
    assert raws[0]["symbol"] == "BTC/USDT"
    assert raws[0]["side"] == "buy"
    assert raws[0]["price"] == 87500.5
    assert raws[0]["phase"] == "entry"
    assert raws[0]["trade_id"] == 1

    # Entry 2 (trade 2, cerrado, long → buy)
    assert raws[1]["symbol"] == "ETH/USDT"
    assert raws[1]["side"] == "buy"
    assert raws[1]["price"] == 3150.25
    assert raws[1]["phase"] == "entry"
    assert raws[1]["trade_id"] == 2

    # Exit (trade 2, cerrado, long → sell)
    assert raws[2]["symbol"] == "ETH/USDT"
    assert raws[2]["side"] == "sell"
    assert raws[2]["price"] == 3230.75
    assert raws[2]["phase"] == "exit"
    assert raws[2]["trade_id"] == 2
    assert raws[2]["exit_reason"] == "roi"

    # Flujo completo: harvest_all escribe 3 eventos al ledger (no agente
    # → NO hay SESSION_SUMMARY).
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source2 = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source2)

    result = h.harvest_all()
    assert result["freqtrade"] == 3

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    etypes = [e["event"]["event_type"] for e in entries]
    assert etypes == ["TRADE_EXECUTED", "TRADE_EXECUTED", "TRADE_EXECUTED"]


# ---------------------------------------------------------------------------
# 3. idempotencia (cursor por max_trade_id)
# ---------------------------------------------------------------------------

def test_two_runs_zero_duplicates(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    r1 = h.harvest_all()
    assert r1["freqtrade"] == 3

    r2 = h.harvest_all()
    assert r2["freqtrade"] == 0, f"Segunda corrida debe dar 0, obtuvo {r2}"

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    ids = {e["event"]["event_id"] for e in entries}
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 4. anti-teatro: cursor no avanza si el write falla
# ---------------------------------------------------------------------------

def test_cursor_not_advanced_on_write_failure(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    import unittest.mock as um
    with um.patch.object(h._writer, "append", side_effect=OSError("disk full")):
        result = h.harvest_all()
    assert "freqtrade" in result
    assert result["freqtrade"] == 0
    # El cursor NO avanzó
    assert (
        not os.path.exists(config)
        or os.path.getsize(config) == 0
        or json.load(open(config)) == {}
    ), "El cursor no debe avanzar si el write falló"

    # Corrida siguiente con write OK → cosecha TODO (3) sin pérdida
    r2 = h.harvest_all()
    assert r2["freqtrade"] == 3
    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    assert len({e["event"]["event_id"] for e in entries}) == 3


# ---------------------------------------------------------------------------
# 5. replay-determinismo (Artículo VI)
# ---------------------------------------------------------------------------

def test_replay_determinism_freqtrade_harvest(tmp_path):
    state1 = _harvest_and_replay(tmp_path, "w1")
    state2 = _harvest_and_replay(tmp_path, "w2")
    # TRADE_EXECUTED es event type custom (registrado, no builtin) →
    # aparece en ``custom_events``, no en ``events_applied``. Ver
    # _replay_engine.py:66 (custom path).
    assert len(state1["custom_events"]) == len(state2["custom_events"])
    assert len(state1["custom_events"]) == 3
    # Determinismo: los 3 custom_events tienen el mismo event_id en ambas
    ids1 = {e["event_id"] for e in state1["custom_events"]}
    ids2 = {e["event_id"] for e in state2["custom_events"]}
    assert ids1 == ids2, f"Event IDs difieren: {ids1} vs {ids2}"
    # Todos son TRADE_EXECUTED
    for ev in state1["custom_events"]:
        assert ev["event_type"] == "TRADE_EXECUTED"


def _harvest_and_replay(tmp_path, name):
    workdir = tmp_path / name
    workdir.mkdir()
    ledger = str(workdir / "ledger.log")
    config = str(workdir / "cursors.json")
    source = _make_source(workdir, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["freqtrade"] == 3
    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["custom_events"]) == 3
    return state


# ---------------------------------------------------------------------------
# 6. anti-teatro: la lectura es read-only
# ---------------------------------------------------------------------------

def test_harvest_does_not_modify_fixture(tmp_path):
    """El harvest solo lee el SQLite: la fixture queda intacta (mismos
    bytes) aunque se coseche varias veces."""
    fixture_path = os.path.join(FIXTURE_DIR, FIXTURE_FILE)
    with open(fixture_path, "rb") as f:
        before = f.read()

    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    assert h.harvest_all()["freqtrade"] == 3
    assert h.harvest_all()["freqtrade"] == 0

    with open(fixture_path, "rb") as f:
        after = f.read()
    assert before == after, "La fixture no debe modificarse"
