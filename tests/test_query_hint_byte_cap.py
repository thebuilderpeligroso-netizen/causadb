"""Tests para extensión BIT-CHR.99 Gap #1 — Hint cuando el BYTE-CAP
oculta eventos recientes (caso real de producción: ledger con pocos
eventos gordos donde el cap de CONTEO no dispara pero el cap de BYTES
trunca los últimos).

Test-First (Art. III): estos tests se escriben ANTES de la
implementación. Verifican que:

1. ``causadb_query`` sin filtros sobre un ledger con eventos gordos
   (cap de bytes pisa, cap de conteo no) setea el hint
   ``result_capped_recents_hidden`` con ``reason: "byte_cap"`` en el
   envelope.
2. Cuando el caller pasa ``from_time`` explícito, NO se setea el hint
   del byte-cap (caller ya sabe qué rango quiere).
3. Cuando el cap de CONTEO también aplica (ledger > 1000 eventos), el
   hint del conteo gana y el byte-cap NO duplica ni pisa el hint
   (anti-teatro: no aparecen dos hints, no se agrega ``reason``).
4. Cuando el ledger es chico (< cap bytes, < cap conteo), NO se setea
   ningún hint (anti-teatro oportuno).

Anti-teatro (Art. IX): cada test tiene poder discriminatorio —
- Un stub que nunca setea el byte_cap_hint falla el test 1.
- Un stub que siempre setea el byte_cap_hint falla los tests 2 y 4.
- Un stub que pisa el hint del conteo con el byte_cap_hint falla el
  test 3 (aparece ``reason`` o cambia el ``max_seq``).
- Un stub que setea el hint cuando el ledger < cap falla el test 4.

Anti-abstracción (Art. VIII): NO se toca ``_apply_byte_cap`` helper.
La lógica del byte_cap_hint vive en el wrapper ``causadb_query`` y
compara ``len(kept) < len(results)`` (post-cap) — no se extiende el
helper ni se crea una nueva abstracción exportada.

No se monkeypatchea ``DEFAULT_QUERY_LIMIT`` global (frágil): se usan
ledgers reales con >1000 eventos para forzar el cap de conteo sin
tocar el global. El cap de bytes se controla vía
``CAUSADB_MAX_RESPONSE_BYTES`` (env, leído call-time).
"""
import json
import os
from types import MappingProxyType

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_index import DEFAULT_QUERY_LIMIT
from causadb._ledger_writer import LedgerWriter
from causadb.mcp._tools import causadb_query


# ---------------------------------------------------------------------------
# Helpers — construir ledgers reales con payloads gordos
# ---------------------------------------------------------------------------

def _seed_gov_ledger_big_payloads(ledger_path: str, n_events: int,
                                  payload_size: int = 60_000) -> None:
    """Escribe ``n_events`` eventos GOVERNANCE_DECISION con un payload
    grande (``reasoning`` de ``payload_size`` chars).

    Cada evento serializado pesa ~payload_size + overhead (~60KB+). Con
    ``CAUSADB_MAX_RESPONSE_BYTES`` bajo (ej: 200000), pocos eventos
    superan el cap de bytes sin alcanzar el cap de conteo (1000).
    """
    writer = LedgerWriter(ledger_path)
    for i in range(n_events):
        e = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="ctx-test",
            source="test",
            timestamp=f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
            payload=MappingProxyType({
                "reasoning": "x" * payload_size,
                "impact": "low",
                "decision_type": "tactical",
                "origin": "agent",
                "idx": i,
            }),
        )
        writer.append(e)


def _seed_gov_ledger_small_payloads(ledger_path: str, n_events: int) -> None:
    """Escribe ``n_events`` eventos GOVERNANCE_DECISION con payload chico.

    Usado para forzar el cap de CONTEO (>1000 eventos) sin disparar el
    cap de bytes (payloads chicos → todos entran en 300KB).
    """
    writer = LedgerWriter(ledger_path)
    for i in range(n_events):
        e = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="ctx-test",
            source="test",
            timestamp=f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
            payload=MappingProxyType({
                "reasoning": f"r-{i}",
                "impact": "low",
                "decision_type": "tactical",
                "origin": "agent",
                "idx": i,
            }),
        )
        writer.append(e)


# ---------------------------------------------------------------------------
# 1. byte-cap pisa, conteo no → hint con reason=byte_cap
# ---------------------------------------------------------------------------

def test_byte_cap_hint_when_results_truncated_and_recents_hidden(
    tmp_path, monkeypatch
):
    """Art. III, IX — ``causadb_query`` sin filtros sobre ledger con
    eventos gordos: el byte-cap trunca los últimos (recientes) sin que
    el cap de conteo dispare. El envelope debe incluir el hint con
    ``reason: "byte_cap"``.

    Anti-teatro: un stub que no implementa el byte_cap_hint falla
    porque ``"hint"`` no está en el envelope (el hint del conteo no
    se setea porque no se alcanza el cap de conteo).
    """
    ledger_path = str(tmp_path / "ledger.log")
    # 10 eventos GOV con payload de 60KB c/u → 5 eventos ~300KB.
    # Con cap de bytes en 200000, se truncan los últimos (recientes).
    # 10 eventos << 1000 (cap de conteo) → el conteo NO dispara.
    _seed_gov_ledger_big_payloads(ledger_path, n_events=10, payload_size=60_000)

    # Cap de bytes bajo para truncamiento determinista.
    monkeypatch.setenv("CAUSADB_MAX_RESPONSE_BYTES", "200000")

    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="GOVERNANCE_DECISION",
        # sin from_time/to_time/text/limit → caller no sabe qué quiere
    )
    data = json.loads(raw)

    assert "hint" in data, (
        f"envelope debe incluir 'hint' cuando byte-cap oculta recientes, "
        f"keys={list(data.keys())}"
    )
    assert data["hint"] == "result_capped_recents_hidden", (
        f"hint debe ser 'result_capped_recents_hidden', "
        f"got {data['hint']!r}"
    )
    assert data.get("reason") == "byte_cap", (
        f"reason debe ser 'byte_cap' para distinguir del count_cap, "
        f"got {data.get('reason')!r}"
    )
    # 10 eventos → seq 0..9 → max_seq = 9.
    assert data["max_seq_in_ledger"] == 9, (
        f"max_seq_in_ledger debe ser 9 (10 eventos, seq 0-9), "
        f"got {data.get('max_seq_in_ledger')}"
    )
    # El byte-cap truncó → last_seq_returned < 9.
    assert data["last_seq_returned"] < 9, (
        f"last_seq_returned debe ser < 9 (byte-cap truncó recientes), "
        f"got {data.get('last_seq_returned')}"
    )
    assert data.get("dropped_events", 0) > 0, (
        f"dropped_events debe ser > 0 (byte-cap truncó), "
        f"got {data.get('dropped_events')}"
    )


# ---------------------------------------------------------------------------
# 2. byte-cap con from_time explícito → NO hint (caller ya filtró)
# ---------------------------------------------------------------------------

def test_byte_cap_no_hint_when_from_time_set(tmp_path, monkeypatch):
    """Art. IX anti-teatro — ``causadb_query`` con ``from_time`` no
    incluye el byte_cap_hint en el envelope (caller explícito).

    Anti-teatro: un stub que siempre setea el byte_cap_hint falla este
    test (from_time explícito debe suprimir el hint).
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_gov_ledger_big_payloads(ledger_path, n_events=10, payload_size=60_000)

    monkeypatch.setenv("CAUSADB_MAX_RESPONSE_BYTES", "200000")

    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="GOVERNANCE_DECISION",
        from_time="1900-01-01T00:00:00Z",  # caller explícito
    )
    data = json.loads(raw)

    assert "hint" not in data, (
        f"from_time explícito no debe incluir byte_cap_hint en envelope, "
        f"keys={list(data.keys())}"
    )
    assert "reason" not in data
    assert "max_seq_in_ledger" not in data
    assert "last_seq_returned" not in data


# ---------------------------------------------------------------------------
# 3. byte-cap NO pisa el hint del conteo (anti-duplicación)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_byte_cap_no_hint_when_count_cap_also_applies(tmp_path, monkeypatch):
    """Art. IX anti-teatro anti-duplicación — cuando el cap de CONTEO
    también aplica (ledger > 1000 eventos), el hint del conteo gana y
    el byte-cap NO debe pisarlo ni agregar ``reason``.

    Escenario: 1100 eventos GOV con payload chico. El cap de conteo
    (1000) dispara primero → ``LedgerIndex.last_query_hint`` se setea.
    El byte-cap puede o no truncar los 1000 devueltos, pero NO debe
    pisar el hint del conteo.

    Anti-teatro: un stub que siempre pisa con byte_cap_hint falla
    porque aparece ``reason`` en el envelope (el count_cap no agrega
    ``reason``) o porque ``max_seq`` cambia.
    """
    ledger_path = str(tmp_path / "ledger.log")
    # 1100 eventos chicos → cap de conteo (1000) dispara.
    _seed_gov_ledger_small_payloads(ledger_path, n_events=1100)

    # Cap de bytes alto para que NO trunque los 1000 devueltos (así
    # aislamos el test: solo el conteo dispara, el byte-cap no).
    monkeypatch.setenv("CAUSADB_MAX_RESPONSE_BYTES", "10000000")

    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="GOVERNANCE_DECISION",
        # sin filtros → ambos caps podrían aplicar
    )
    data = json.loads(raw)

    assert "hint" in data, (
        f"envelope debe incluir 'hint' del count_cap (1100 > 1000), "
        f"keys={list(data.keys())}"
    )
    assert data["hint"] == "result_capped_recents_hidden", (
        f"hint debe ser 'result_capped_recents_hidden' (del count_cap), "
        f"got {data['hint']!r}"
    )
    # Anti-duplicación: el count_cap NO agrega ``reason`` (back-compat
    # con el fix previo). Si el byte_cap_hint pisara, aparecería
    # ``reason: "byte_cap"`` — eso es un bug.
    assert "reason" not in data, (
        f"count_cap no agrega 'reason'; si aparece, el byte_cap_hint "
        f"pisó el hint del conteo (bug anti-duplicación), "
        f"keys={list(data.keys())}"
    )
    # 1100 eventos → seq 0..1099 → max_seq = 1099.
    assert data["max_seq_in_ledger"] == 1099, (
        f"max_seq_in_ledger debe ser 1099 (1100 eventos), "
        f"got {data.get('max_seq_in_ledger')}"
    )
    # Cap de conteo devuelve seq 0..999 → last_seq_returned = 999.
    assert data["last_seq_returned"] == 999, (
        f"last_seq_returned debe ser 999 (count_cap devuelve seq 0-999), "
        f"got {data.get('last_seq_returned')}"
    )


# ---------------------------------------------------------------------------
# 4. ledger chico → NO hint (anti-teatro oportuno)
# ---------------------------------------------------------------------------

def test_byte_cap_no_hint_when_ledger_small(tmp_path, monkeypatch):
    """Art. IX anti-teatro oportuno — ledger chico (< cap bytes, < cap
    conteo) no setea ningún hint porque no hay recientes ocultos.

    Anti-teatro: un stub que setea el byte_cap_hint siempre que
    ``len(kept) < len(results)`` (sin chequear truncamiento) falla
    este test (5 eventos, no se trunca nada).
    """
    ledger_path = str(tmp_path / "ledger.log")
    # 5 eventos GOV chicos → no alcanzan ningún cap.
    _seed_gov_ledger_small_payloads(ledger_path, n_events=5)

    # Cap de bytes default (300KB) → 5 eventos chicos entran todos.
    monkeypatch.delenv("CAUSADB_MAX_RESPONSE_BYTES", raising=False)

    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="GOVERNANCE_DECISION",
    )
    data = json.loads(raw)

    assert "hint" not in data, (
        f"ledger chico no debe setear hint (no hay recientes ocultos), "
        f"keys={list(data.keys())}"
    )
    assert "reason" not in data
    assert data.get("truncated") is False, (
        f"ledger chico no debe truncar, got truncated={data.get('truncated')}"
    )
    assert len(data["events"]) == 5, (
        f"5 eventos GOV deben devolverse todos, got {len(data['events'])}"
    )
