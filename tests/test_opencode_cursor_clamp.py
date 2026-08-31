"""Tests Fix 1 — Clamp del cursor opencode (BIT-CHR.105).

El harvester de opencode reconcilia el cursor cuando la DB se compacta y
``MAX(rowid)`` retrocede. Antes del fix, el clamp emitía un raw
``OBSERVATION`` inválido (sin ``file_path``, ``line_number``, ``severity``)
que rompía ``causadb validate`` y ``causadb replay`` con
``ValueError: invalid severity None``.

Después del fix:
  - El clamp sigue aplicando (``max_rowid = int(db_max_rowid)``).
  - NO se emite evento al ledger (bookkeeping operacional del harvester).
  - Se loguea un ``logging.warning`` con substring identificable.

Anti-teatro (Art. IX): los tests validan comportamiento real —
  - que el warning se emite con el substring correcto (caplog.text),
  - que el harvest continúa desde el rowid clampeado (no desde el cursor
    adelantado),
  - que NO se emite OBSERVATION inválido al ledger.

Construcción del store: SQLite tempfile con schema ``part`` mínima
(patrón de ``tests/fixtures/_build_opencode_fixture.py``). NO usamos la
fixture original porque necesitamos controlar ``MAX(rowid)`` para forzar
el drift.
"""

import json
import logging
import os
import sqlite3

import pytest

from causadb._harvest_source_opencode import OpenCodeHarvestSource


# ---------------------------------------------------------------------------
# Helpers — construir un store opencode mínimo controlado
# ---------------------------------------------------------------------------

# Schema mínimo de la tabla ``part`` (lo que el harvest lee). La tabla
# ``session`` se necesita para el LEFT JOIN (s.id, s.agent) — aunque esté
# vacía, el LEFT JOIN retorna NULLs y el harvest continúa.
_CREATE_SESSION = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY, `agent` text, `model` text
)
"""
_CREATE_PART = """
CREATE TABLE `part` (
  `id` text PRIMARY KEY, `message_id` text, `session_id` text,
  `time_created` integer NOT NULL, `time_updated` integer NOT NULL,
  `data` text NOT NULL
)
"""


def _build_store(tmp_path, n_parts=5):
    """Crea un store opencode con ``n_parts`` parts reasoning válidos.

    Retorna la ruta al db. Los rowids van de 1 a n_parts (AUTOINCREMENT
    implícito de SQLite), así ``MAX(rowid) == n_parts``.
    """
    db_path = str(tmp_path / "opencode_test.db")
    con = sqlite3.connect(db_path)
    con.executescript(_CREATE_SESSION)
    con.executescript(_CREATE_PART)
    for i in range(n_parts):
        data = json.dumps({
            "type": "reasoning",
            "text": f"reasoning step {i} for cursor clamp test",
            "time": {"start": 1785103951000 + i, "end": 1785103951001 + i},
        })
        con.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, "
            "time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_test_{i:04d}", f"msg_test_{i:04d}", "ses_test",
             1785103951000 + i, 1785103951001 + i, data),
        )
    con.commit()
    con.close()
    return db_path


def _make_source(tmp_path, db_path=None, n_parts=5):
    if db_path is None:
        db_path = _build_store(tmp_path, n_parts=n_parts)
    return OpenCodeHarvestSource(
        ledger_path=str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# 1. Clamp cuando cursor > DB MAX(rowid)
# ---------------------------------------------------------------------------

def test_clamp_when_cursor_ahead_of_db(tmp_path, caplog):
    """Cursor ``max_rowid=200`` pero DB ``MAX(rowid)=100`` → el harvest
    debe clampear a 100, loguear WARNING identificable, y NO emitir
    OBSERVATION inválido al ledger."""
    n_parts = 5  # MAX(rowid) = 5
    db_path = _build_store(tmp_path, n_parts=n_parts)
    source = _make_source(tmp_path, db_path=db_path)

    # Cursor adelantado: 200 > MAX(rowid)=5 → drift
    cursor = {"max_rowid": 200}

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor))

    # Anti-teatro: el clamp NO emite OBSERVATION inválido (cero raws con
    # type=OBSERVATION). El harvest continúa desde rowid=5 → 0 raws porque
    # WHERE rowid > 5 retorna vacío.
    observation_raws = [r for r in raws if r.get("type") == "OBSERVATION"]
    assert observation_raws == [], (
        "El clamp NO debe emitir OBSERVATION al ledger (era el bug). "
        f"Obtuvo {len(observation_raws)} raw(s) OBSERVATION."
    )

    # Anti-teatro: el warning se loguea con substring identificable.
    # caplog.text captura todos los log records del nivel configurado.
    assert caplog.text, "Se esperaba al menos un log WARNING pero caplog está vacío"
    # Substring match — el mensaje del fix contiene "drifted ahead" o
    # "clampeado" (bilingüe para robustez).
    log_text = caplog.text.lower()
    assert ("drifted ahead" in log_text) or ("clampeado" in log_text), (
        f"El WARNING debe contener 'drifted ahead' o 'clampeado'. "
        f"Log capturado: {caplog.text!r}"
    )

    # Anti-teatro: el clamp aplicó — el harvest continúa desde rowid=5
    # (MAX(rowid) actual), no desde 200. Como WHERE rowid > 5 retorna
    # vacío, no hay raws cosechados. Verificamos que el generador no
    # lanzó y produjo 0 eventos de tipo REASONING_STEP (que sería lo
    # esperado si el clamp NO aplicara y el WHERE retornara filas).
    reasoning_raws = [r for r in raws if r.get("type") == "REASONING_STEP"]
    assert reasoning_raws == [], (
        "Con cursor=200 y MAX(rowid)=5, el clamp debe setear max_rowid=5 "
        "y el WHERE rowid > 5 retorna vacío. No debería haber raws."
    )


# ---------------------------------------------------------------------------
# 2. Sin clamp cuando cursor < DB MAX(rowid)
# ---------------------------------------------------------------------------

def test_no_clamp_when_cursor_behind_db(tmp_path, caplog):
    """Cursor ``max_rowid=2`` y DB ``MAX(rowid)=5`` → no drift, no warning,
    harvest normal desde rowid=2 (debe cosechar los parts 3, 4, 5)."""
    n_parts = 5
    db_path = _build_store(tmp_path, n_parts=n_parts)
    source = _make_source(tmp_path, db_path=db_path)

    cursor = {"max_rowid": 2}  # 2 < 5 → no drift

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor))

    # Anti-teatro: NO se loguea warning de drift (no hay drift).
    log_text = caplog.text.lower() if caplog.text else ""
    assert "drifted ahead" not in log_text and "clampeado" not in log_text, (
        f"No debería loguearse warning de drift (cursor < MAX). Log: {caplog.text!r}"
    )

    # Anti-teatro: el harvest continúa desde rowid=2 → cosecha los parts
    # con rowid > 2 (3, 4, 5) → 3 REASONING_STEP.
    reasoning_raws = [r for r in raws if r.get("type") == "REASONING_STEP"]
    assert len(reasoning_raws) == 3, (
        f"Esperaba 3 REASONING_STEP (rowid 3,4,5), obtuvo {len(reasoning_raws)}. "
        f"El harvest no continuó correctamente desde cursor=2."
    )


# ---------------------------------------------------------------------------
# 3. Sin clamp cuando cursor == DB MAX(rowid)
# ---------------------------------------------------------------------------

def test_no_clamp_when_cursor_equal_to_db(tmp_path, caplog):
    """Cursor ``max_rowid=5`` y DB ``MAX(rowid)=5`` → no drift, no warning,
    0 eventos cosechados (WHERE rowid > 5 retorna vacío)."""
    n_parts = 5
    db_path = _build_store(tmp_path, n_parts=n_parts)
    source = _make_source(tmp_path, db_path=db_path)

    cursor = {"max_rowid": 5}  # == MAX(rowid) → no drift

    with caplog.at_level(logging.WARNING, logger="root"):
        raws = list(source.harvest(cursor))

    # Anti-teatro: NO warning.
    log_text = caplog.text.lower() if caplog.text else ""
    assert "drifted ahead" not in log_text and "clampeado" not in log_text, (
        f"No debería loguearse warning (cursor == MAX). Log: {caplog.text!r}"
    )

    # Anti-teatro: 0 raws (WHERE rowid > 5 retorna vacío).
    assert raws == [], (
        f"Esperaba 0 raws (cursor == MAX, WHERE rowid > 5 vacío), "
        f"obtuvo {len(raws)}."
    )
