"""Tests Fase B-1 — Enriquecimiento TRADE_EXECUTED con indicadores entry/exit.

Sidecar: CSV de ``freqtrade backtesting-analysis --analysis-to-csv``
(env ``CAUSADB_FREQTRADE_ANALYSIS_CSV``). Backward compatible: sin
sidecar o sin match, el harvest cosecha como hoy (claves ausentes,
nunca None). Fail-closed: sidecar roto → warning + sin indicators,
nunca aborta el harvest. Read-only: ni el sqlite ni el CSV se modifican.

CONTRATO CSV VERIFICADO (fuente real, no inventado — freqtrade no está
instalado en este env, así que se verificó contra upstream):
- Código: freqtrade/develop ``freqtrade/data/entryexitanalysis.py``,
  función ``_merge_dfs``: ``merge_on = ["pair", "open_date"]``,
  ``columns_to_keep = [*merge_on, "enter_reason", "exit_reason"]``,
  merge con ``suffixes=(" (entry)", " (exit)")``.
- Docs oficiales (advanced-backtesting): tabla de ejemplo con header
  ``pair | open_date | enter_reason | exit_reason |
  chikou_span (entry) | tenkan_sen (entry) |
  chikou_span (exit) | tenkan_sen (exit)`` y 2 filas reales:
  ``DOGE/USDT | 2024-07-06 00:35:00+00:00 |  | exit_signal |
  0.105 | 0.106 | 0.105 | 0.107``
  ``BTC/USDT | 2024-08-05 14:20:00+00:00 |  | roi |
  54643.440 | 51696.400 | 54386.000 | 52072.010``
- Campos trade-wide accesibles vía --indicator-list (incl. ``close_date``).
- Fixture sintética abajo = ese header exacto recortado a 7 columnas
  (pair,open_date,close_date,rsi/ema_9 entry+exit) — ``close_date`` se
  agrega porque el spec exige match exit↔close_date nunca cruzado, y es
  un campo trade-wide documentado.
"""

import json
import logging
import os
import shutil

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_freqtrade import (
    FreqtradeHarvestSource,
    _lookup_indicators,
)
from causadb._replay_engine import ReplayEngine

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_FILE = "freqtrade_fixture.sqlite"

ALLOWLIST = "rsi,ema_9"

# Fixture sidecar: header real recortado (ver docstring del módulo).
# Fechas con zona "+00:00" y con milisegundos a propósito: el match debe
# normalizar ambos lados (strip zona/milisegundos).
SIDECAR_HEADER = (
    "pair,open_date,close_date,"
    "rsi (entry),rsi (exit),ema_9 (entry),ema_9 (exit)\n"
)
SIDECAR_ROWS = (
    # Trade 2 de la fixture sqlite (ETH/USDT): entry+exit.
    "ETH/USDT,2026-06-01 14:00:01+00:00,2026-07-15 10:30:05+00:00,"
    "42.5,61.2,3180.1,3210.4\n"
    # Trade 1 (BTC/USDT, abierto): solo entry, sin close.
    "BTC/USDT,2026-08-01 08:30:00+00:00,,35.0,,87600.2,\n"
)

EXPECTED_ENTRY_ETH = {"rsi": 42.5, "ema_9": 3180.1}
EXPECTED_EXIT_ETH = {"rsi": 61.2, "ema_9": 3210.4}
EXPECTED_ENTRY_BTC = {"rsi": 35.0, "ema_9": 87600.2}


def _install_fixture(tmp_path):
    dst = tmp_path / "tradesv3.sqlite"
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_FILE), dst)
    return str(dst)


def _make_source(tmp_path, ledger_path=None, db_path=None):
    if db_path is None:
        db_path = _install_fixture(tmp_path)
    return FreqtradeHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


def _write_sidecar(tmp_path, name="group_0.csv", content=None):
    p = tmp_path / name
    p.write_text(SIDECAR_HEADER + (content if content is not None else SIDECAR_ROWS),
                 encoding="utf-8")
    return str(p)


def _env_sidecar(monkeypatch, csv_path, indicators=ALLOWLIST):
    monkeypatch.setenv("CAUSADB_FREQTRADE_ANALYSIS_CSV", csv_path)
    monkeypatch.setenv("CAUSADB_FREQTRADE_INDICATORS", indicators)


# ---------------------------------------------------------------------------
# 1. Enriquecimiento entry/exit + persistencia real en ledger (anti-teatro)
# ---------------------------------------------------------------------------

def test_indicators_enrichment_entry_exit_and_ledger_persisted(tmp_path, monkeypatch):
    csv_path = _write_sidecar(tmp_path)
    csv_before = open(csv_path, "rb").read()
    _env_sidecar(monkeypatch, csv_path)

    source = _make_source(tmp_path)
    raws = source.harvest(None)
    assert len(raws) == 3  # mismo harvest que hoy, solo enriquecido
    by_phase = {(r["symbol"], r["phase"]): r for r in raws}

    entry_eth = by_phase[("ETH/USDT", "entry")]
    assert entry_eth["indicators_entry"] == EXPECTED_ENTRY_ETH
    assert list(entry_eth["indicators_entry"].keys()) == ["rsi", "ema_9"]
    assert entry_eth["indicators_provenance"] == "backtest-analysis-csv"

    exit_eth = by_phase[("ETH/USDT", "exit")]
    assert exit_eth["indicators_entry"] == EXPECTED_EXIT_ETH
    assert exit_eth["indicators_provenance"] == "backtest-analysis-csv"

    entry_btc = by_phase[("BTC/USDT", "entry")]
    assert entry_btc["indicators_entry"] == EXPECTED_ENTRY_BTC

    # Sin sidecar → claves ausentes (no None) — backward compatible.
    monkeypatch.delenv("CAUSADB_FREQTRADE_ANALYSIS_CSV")
    plain = _make_source(tmp_path).harvest(None)
    assert len(plain) == 3
    for r in plain:
        assert "indicators_entry" not in r
        assert "indicators_provenance" not in r

    # Anti-teatro: el ledger persistido contiene los indicators (no solo
    # memoria). Lectura igual que test existente líneas 129-136.
    _env_sidecar(monkeypatch, csv_path)
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    h = Harvester(ledger, config)
    h.register_source(_make_source(tmp_path, ledger))
    assert h.harvest_all()["freqtrade"] == 3

    with open(ledger) as f:
        entries = [json.loads(ln) for ln in f if ln.strip()]
    assert len(entries) == 3
    persisted = {(e["event"]["payload"]["symbol"], e["event"]["payload"]["phase"]):
                 e["event"]["payload"] for e in entries}
    assert persisted[("ETH/USDT", "entry")]["indicators_entry"] == EXPECTED_ENTRY_ETH
    assert persisted[("ETH/USDT", "entry")]["indicators_provenance"] == "backtest-analysis-csv"
    assert persisted[("ETH/USDT", "exit")]["indicators_entry"] == EXPECTED_EXIT_ETH
    assert persisted[("BTC/USDT", "entry")]["indicators_entry"] == EXPECTED_ENTRY_BTC

    # Read-only: el CSV no se modificó.
    assert open(csv_path, "rb").read() == csv_before


# ---------------------------------------------------------------------------
# 2. Tolerancia ±1 vela sí / ±2 velas no (+ empate determinista)
# ---------------------------------------------------------------------------

def test_lookup_tolerance_one_candle_yes_two_no(tmp_path, monkeypatch):
    csv_path = _write_sidecar(tmp_path)
    _env_sidecar(monkeypatch, csv_path)
    from causadb._harvest_source_freqtrade import _load_signals_csv
    signals = _load_signals_csv(csv_path, ["rsi", "ema_9"])
    assert signals, "el sidecar debe cargar señales"

    # ±1 vela (300s con timeframe 5m=300s) SÍ matchea.
    assert _lookup_indicators("ETH/USDT", "2026-06-01T14:05:01", signals, 300) == EXPECTED_ENTRY_ETH
    assert _lookup_indicators("ETH/USDT", "2026-06-01T13:55:01", signals, 300) == EXPECTED_ENTRY_ETH
    # ±2 velas (600s) NO matchea.
    assert _lookup_indicators("ETH/USDT", "2026-06-01T14:10:01", signals, 300) is None
    assert _lookup_indicators("ETH/USDT", "2026-06-01T13:50:01", signals, 300) is None
    # Exacto sigue funcionando.
    assert _lookup_indicators("ETH/USDT", "2026-06-01T14:00:01", signals, 300) == EXPECTED_ENTRY_ETH
    # Otro pair no cruza aunque el ts coincida.
    assert _lookup_indicators("XRP/USDT", "2026-06-01T14:00:01", signals, 300) is None
    # Exit matchea por close_date, nunca cruzado con open_date.
    assert _lookup_indicators("ETH/USDT", "2026-07-15T10:30:05", signals, 300) == EXPECTED_EXIT_ETH

    # Empate |delta| → primera fila (determinista).
    tie_csv = tmp_path / "tie.csv"
    tie_csv.write_text(
        "pair,open_date,rsi (entry)\n"
        "ETH/USDT,2026-06-01 14:00:00+00:00,11.0\n"
        "ETH/USDT,2026-06-01 14:10:00+00:00,22.0\n",
        encoding="utf-8",
    )
    tie = _load_signals_csv(str(tie_csv), ["rsi"])
    assert _lookup_indicators("ETH/USDT", "2026-06-01T14:05:00", tie, 600) == {"rsi": 11.0}


# ---------------------------------------------------------------------------
# 3. Orden keys == allowlist + determinismo event_ids + cursor idéntico
# ---------------------------------------------------------------------------

def test_order_determinism_and_identical_cursor(tmp_path, monkeypatch):
    csv_path = _write_sidecar(tmp_path)
    _env_sidecar(monkeypatch, csv_path, "ema_9,rsi")

    raws = _make_source(tmp_path).harvest(None)
    entry_eth = next(r for r in raws if r["symbol"] == "ETH/USDT" and r["phase"] == "entry")
    # Orden indicators = orden allowlist (invertido a propósito).
    assert list(entry_eth["indicators_entry"].keys()) == ["ema_9", "rsi"]
    assert entry_eth["indicators_entry"] == {"ema_9": 3180.1, "rsi": 42.5}

    # Determinismo: 2 harvests con mismo CSV → mismos event_ids.
    def _harvest_ids(workdir):
        ledger = str(workdir / "ledger.log")
        h = Harvester(ledger, str(workdir / "cursors.json"))
        h.register_source(_make_source(workdir, ledger))
        assert h.harvest_all()["freqtrade"] == 3
        with open(ledger) as f:
            return [json.loads(ln)["event"]["event_id"] for ln in f if ln.strip()]

    d1, d2 = tmp_path / "w1", tmp_path / "w2"
    d1.mkdir()
    d2.mkdir()
    shutil.copy(str(tmp_path / "tradesv3.sqlite"), d1 / "tradesv3.sqlite")
    shutil.copy(str(tmp_path / "tradesv3.sqlite"), d2 / "tradesv3.sqlite")
    # El sidecar se resuelve por env: mismo contenido, distinto path.
    _env_sidecar(monkeypatch, csv_path, "ema_9,rsi")
    assert _harvest_ids(d1) == _harvest_ids(d2)

    # Cursor idéntico con/sin sidecar.
    src = _make_source(tmp_path)
    cur_with = src.advance_cursor(None, src.harvest(None))
    monkeypatch.delenv("CAUSADB_FREQTRADE_ANALYSIS_CSV")
    monkeypatch.delenv("CAUSADB_FREQTRADE_INDICATORS")
    src2 = _make_source(tmp_path)
    cur_without = src2.advance_cursor(None, src2.harvest(None))
    assert cur_with == {"max_trade_id": 2}
    assert cur_without == {"max_trade_id": 2}
    assert cur_with == cur_without


# ---------------------------------------------------------------------------
# 4. Guardas fail-closed: cosechar como hoy, nunca abortar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", [
    "missing", "directory", "non_utf8", "no_pair", "no_open_date",
    "too_many_rows", "too_many_bytes",
])
def test_guards_fail_closed_without_indicators(tmp_path, monkeypatch, caplog, case):
    if case == "missing":
        csv_path = str(tmp_path / "no-existe.csv")
    elif case == "directory":
        csv_path = str(tmp_path)
    elif case == "non_utf8":
        csv_path = str(tmp_path / "bad.csv")
        with open(csv_path, "wb") as f:
            f.write("pair,open_date\n\xff\xfe binary \x80 invalid".encode("latin-1"))
    elif case == "no_pair":
        csv_path = str(tmp_path / "bad.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("open_date,rsi (entry)\n2026-06-01 14:00:01+00:00,42.5\n")
    elif case == "no_open_date":
        csv_path = str(tmp_path / "bad.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("pair,rsi (entry)\nETH/USDT,42.5\n")
    elif case == "too_many_rows":
        csv_path = str(tmp_path / "big.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("pair,open_date,rsi (entry)\n")
            for i in range(50001):
                f.write(f"ETH/USDT,2026-06-01 14:00:01+00:00,{(i % 90) + 1}.0\n")
    elif case == "too_many_bytes":
        csv_path = str(tmp_path / "huge.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("pair,open_date,nota,rsi (entry)\n")
            f.write("ETH/USDT,2026-06-01 14:00:01+00:00," + "x" * (21 * 1024 * 1024) + ",42.5\n")

    monkeypatch.setenv("CAUSADB_FREQTRADE_ANALYSIS_CSV", csv_path)
    monkeypatch.setenv("CAUSADB_FREQTRADE_INDICATORS", ALLOWLIST)

    with caplog.at_level(logging.WARNING):
        raws = _make_source(tmp_path).harvest(None)

    # Se cosecha como hoy (3 eventos), sin indicators, con warning.
    assert len(raws) == 3, f"caso {case}: el harvest no debe abortar"
    for r in raws:
        assert "indicators_entry" not in r, f"caso {case}: clave debe estar ausente, no None"
        assert "indicators_provenance" not in r
    assert "warning" in caplog.text.lower(), f"caso {case}: se esperaba un warning"
