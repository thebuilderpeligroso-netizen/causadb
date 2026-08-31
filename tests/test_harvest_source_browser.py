"""Tests — BrowserHistorySource (harvest de historial Chrome/Firefox).

Cobertura:
  1. detect() True con un sqlite Chrome válido
  2. harvest → raw events OBSERVATION con severity='info' (R.1.3: el raw
     event es replay-safe desde el origen — BIT-CHR.34)
  3. flujo completo harvest → replay sin ValueError (regresión del bug
     revive: observaciones de browser no deben romper reconstruct_state)
"""

import sqlite3

from causadb._harvest_source_browser import BrowserHistorySource
from causadb._harvester import Harvester
from causadb._replay_engine import ReplayEngine


def _make_chrome_db(tmp_path):
    """Crea un sqlite con schema de Chrome History y 2 urls."""
    db_path = tmp_path / "History"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, last_visit_time INTEGER)")
    conn.execute("INSERT INTO urls (url, title, last_visit_time) VALUES (?, ?, ?)",
                 ("https://example.com", "Example", 13150000000000000))
    conn.execute("INSERT INTO urls (url, title, last_visit_time) VALUES (?, ?, ?)",
                 ("https://test.org", "Test", 13150000000000001))
    conn.commit()
    conn.close()
    return str(db_path)


def _make_source(tmp_path):
    return BrowserHistorySource(browser_paths=[_make_chrome_db(tmp_path)])


# ---------------------------------------------------------------------------
# 1. detect()
# ---------------------------------------------------------------------------

def test_detect_true_with_db(tmp_path):
    source = _make_source(tmp_path)
    assert source.detect() is True
    assert source.source_type() == "browser"
    assert source.cursor_key() == "browser_history"


# ---------------------------------------------------------------------------
# 2. harvest → raw events con severity='info'
# ---------------------------------------------------------------------------

def test_harvest_browser_emits_valid_severity(tmp_path):
    """El raw event de BrowserHistorySource incluye severity='info': el
    evento que viaja al ledger es replay-safe desde el origen (BIT-CHR.34)."""
    source = _make_source(tmp_path)
    events = source.harvest()
    assert len(events) == 2
    for ev in events:
        assert ev["type"] == "OBSERVATION"
        assert ev["severity"] == "info", (
            f"raw event must carry severity='info', got {ev['severity']!r}"
        )


# ---------------------------------------------------------------------------
# 3. flujo completo harvest → replay
# ---------------------------------------------------------------------------

def test_harvest_browser_replay_without_valueerror(tmp_path):
    """Flujo completo harvest → replay no lanza ValueError (regresión del
    bug revive con observaciones de browser sin file_path)."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")
    source = _make_source(tmp_path)
    h = Harvester(ledger, config)
    h.register_source(source)
    assert h.harvest_all()["browser"] == 2
    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["observations"]) == 2
    assert all(o["severity"] == "info" for o in state["observations"])
