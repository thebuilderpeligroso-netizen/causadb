"""Tests Fix 2 — Hermes: validar tablas antes de PRAGMA table_info.

Bug: en ``_harvest_source_hermes.py`` el método ``_harvest()`` hace
``PRAGMA table_info(sessions)`` y ``PRAGMA table_info(messages)`` asumiendo
que las tablas existen. Si hay un SQLite corrupto/vacío en ``db_path``
lanzan ``sqlite3.OperationalError: no such table: messages`` o
``sessions``. Aunque ``harvest_all`` aísla la falla por fuente, llena el
log de errores.

Fix: antes del PRAGMA, verificar que ambas tablas existan en
``sqlite_master``. Si no existen, loguear warning y returnar temprano
(fail-open silencioso).

Anti-teatro (Art. IX): los tests validan comportamiento real —
  - que NO se lanza ``sqlite3.OperationalError`` con tabla missing,
  - que el generador retorna vacío (no raise, no eventos),
  - que se loguea WARNING con substring identificable,
  - que con tablas presentes el harvest sigue cosechando (eventos > 0).
"""

import logging
import os
import shutil
import sqlite3

import pytest

from causadb._harvest_source_hermes import HermesHarvestSource

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "hermes_fixture.db"


# ---------------------------------------------------------------------------
# Helpers — construir stores Hermes con tablas selectivas
# ---------------------------------------------------------------------------

# Schema mínimo de ``sessions`` (lo mínimo para que el PRAGMA no falle).
_CREATE_SESSIONS = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    model TEXT,
    started_at REAL NOT NULL
)
"""

# Schema mínimo de ``messages`` (lo mínimo para que el PRAGMA no falle
# y el harvest continúe). El harvest hace SELECT de muchas columnas pero
# usa ``NULL AS <col>`` para las que no existen (patrón del archivo).
_CREATE_MESSAGES = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL
)
"""


def _build_store_with_sessions_only(tmp_path):
    """Crea un store con tabla ``sessions`` pero SIN ``messages``."""
    db_path = str(tmp_path / "hermes_sessions_only.db")
    con = sqlite3.connect(db_path)
    con.executescript(_CREATE_SESSIONS)
    con.execute(
        "INSERT INTO sessions (id, model, started_at) VALUES (?, ?, ?)",
        ("ses_test_1", "test-model", 1785676577.0),
    )
    con.commit()
    con.close()
    return db_path


def _build_store_with_messages_only(tmp_path):
    """Crea un store con tabla ``messages`` pero SIN ``sessions``."""
    db_path = str(tmp_path / "hermes_messages_only.db")
    con = sqlite3.connect(db_path)
    con.executescript(_CREATE_MESSAGES)
    con.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        ("ses_test_1", "user", "hello", 1785676577.0),
    )
    con.commit()
    con.close()
    return db_path


def _build_empty_store(tmp_path):
    """Crea un SQLite vacío (sin tablas)."""
    db_path = str(tmp_path / "hermes_empty.db")
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE _dummy (x INTEGER)")  # para que el file exista
    con.commit()
    con.close()
    return db_path


def _install_real_fixture(tmp_path):
    """Copia la fixture real de Hermes (state.db recortado) a tmp_path."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(db_path, tmp_path):
    return HermesHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# 1. Skip cuando messages falta (sessions presente)
# ---------------------------------------------------------------------------

def test_harvest_skips_when_messages_table_missing(tmp_path, caplog):
    """Store con ``sessions`` pero SIN ``messages`` → el harvest NO lanza
    ``sqlite3.OperationalError``, retorna generador vacío, y loguea
    WARNING con substring identificable."""
    db_path = _build_store_with_sessions_only(tmp_path)
    source = _make_source(db_path, tmp_path)

    with caplog.at_level(logging.WARNING, logger="root"):
        # Anti-teatro: el generador debe consumirse sin lanzar.
        raws = list(source.harvest(cursor={}))

    # Anti-teatro: 0 eventos (fail-open silencioso).
    assert raws == [], (
        f"Esperaba 0 raws (messages missing → skip), obtuvo {len(raws)}."
    )

    # Anti-teatro: WARNING con substring identificable.
    assert caplog.text, "Se esperaba WARNING pero caplog está vacío"
    log_text = caplog.text.lower()
    assert "missing required tables" in log_text, (
        f"El WARNING debe contener 'missing required tables'. "
        f"Log capturado: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 2. Skip cuando sessions falta (messages presente)
# ---------------------------------------------------------------------------

def test_harvest_skips_when_sessions_table_missing(tmp_path, caplog):
    """Store con ``messages`` pero SIN ``sessions`` → mismo comportamiento
    fail-open que el test anterior."""
    db_path = _build_store_with_messages_only(tmp_path)
    source = _make_source(db_path, tmp_path)

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor={}))

    assert raws == [], (
        f"Esperaba 0 raws (sessions missing → skip), obtuvo {len(raws)}."
    )

    assert caplog.text, "Se esperaba WARNING pero caplog está vacío"
    log_text = caplog.text.lower()
    assert "missing required tables" in log_text, (
        f"El WARNING debe contener 'missing required tables'. "
        f"Log capturado: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 3. Skip cuando ambas tablas faltan (store vacío)
# ---------------------------------------------------------------------------

def test_harvest_skips_when_both_tables_missing(tmp_path, caplog):
    """Store vacío (sin tablas) → fail-open, 0 eventos, WARNING."""
    db_path = _build_empty_store(tmp_path)
    source = _make_source(db_path, tmp_path)

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor={}))

    assert raws == [], (
        f"Esperaba 0 raws (store vacío → skip), obtuvo {len(raws)}."
    )

    assert caplog.text, "Se esperaba WARNING pero caplog está vacío"
    log_text = caplog.text.lower()
    assert "missing required tables" in log_text, (
        f"El WARNING debe contener 'missing required tables'. "
        f"Log capturado: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 4. Anti-teatro: con tablas presentes el harvest sigue cosechando
# ---------------------------------------------------------------------------

def test_harvest_continues_when_both_tables_present(tmp_path, caplog):
    """Store con ambas tablas (fixture real) → el harvest NO skipea,
    yields eventos REALES (> 0). Anti-teatro: validar que se producen
    eventos, no 0."""
    db_path = _install_real_fixture(tmp_path)
    source = _make_source(db_path, tmp_path)

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor={}))

    # Anti-teatro: el harvest sigue cosechando — eventos > 0.
    assert len(raws) > 0, (
        "Con ambas tablas presentes (fixture real), el harvest debe "
        f"producir eventos > 0. Obtuvo {len(raws)}."
    )

    # Anti-teatro: NO se loguea warning de missing tables (las tablas existen).
    log_text = caplog.text.lower() if caplog.text else ""
    assert "missing required tables" not in log_text, (
        f"No debería loguearse 'missing required tables' (tablas presentes). "
        f"Log: {caplog.text!r}"
    )

    # Anti-teatro adicional: los raws tienen type válido (no son None, no
    # son strings vacíos). Confirma que el harvest realmente procesó filas.
    types_seen = {r.get("type") for r in raws}
    valid_types = {
        "REASONING_STEP", "TOOL_CALLED", "LLM_INVOKED",
        "SESSION_STARTED", "SESSION_ENDED", "API_ATTEMPT",
        "COST_ACCOUNTED",
    }
    assert types_seen & valid_types, (
        f"Los raws deben tener type válido de Hermes. Obtuvo types: {types_seen}."
    )
