"""Tests for BIT-CHR.99 Gap #1 — Hint cuando el cap oculta eventos recientes.

Test-First (Art. III): estos tests se escriben ANTES de la implementación.
Verifican que:

1. ``LedgerIndex.query`` sin ``from_time``/``to_time``/``text``/``limit``
   sobre un ledger > cap setea ``last_query_hint`` avisando que hay
   eventos recientes ocultos (caso real documentado en BIT-CHR.99:
   operador ve eventos del Génesis en vez de los últimos).
2. Cuando el caller pasa ``from_time``/``to_time``/``text``/``limit``
   explícito, NO se setea el hint (caller ya sabe qué quiere).
3. Cuando el ledger es más chico que el cap, NO se setea el hint
   (no hay recientes ocultos).
4. El hint se resetea en cada call (no stale state en LedgerIndex
   compartido — C1b del auditor).
5. La tool MCP ``causadb_query`` propaga el hint en el envelope JSON.
6. El CLI ``causadb query`` propaga ``--limit`` y emite el hint a stderr.

Anti-teatro (Art. IX): cada test tiene poder discriminatorio —
- Un stub que siempre setea el hint falla los tests de "no hint con filtros".
- Un stub que nunca setea el hint falla los tests de "hint seteado sin filtros".
- Un stub que no resetea el hint falla el test de stale state.
- Un stub que setea el hint cuando el ledger < cap falla el test anti-teatro.

Anti-abstracción (Art. VIII): el helper ``_maybe_set_recent_hint`` es
private al módulo ``_ledger_index`` (prefijo ``_``), no es una nueva
abstracción exportada — es un refactor interno de ``query()``.

No se monkeypatchea ``DEFAULT_QUERY_LIMIT`` global (frágil): se usan
ledgers reales con >1000 eventos para forzar el cap sin tocar el global.
"""
import json
import sys
from argparse import Namespace
from io import StringIO

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_index import LedgerIndex, DEFAULT_QUERY_LIMIT
from causadb._ledger_writer import LedgerWriter
from causadb.mcp._tools import causadb_query
from causadb.cli._cmd_query import cmd_query


# ---------------------------------------------------------------------------
# Helpers — construir ledgers reales sin tocar el global DEFAULT_QUERY_LIMIT
# ---------------------------------------------------------------------------

def _seed_ledger(ledger_path: str, n_events: int, payload_path: str = "/x.txt") -> None:
    """Escribe ``n_events`` eventos FILE_MODIFIED con timestamps crecientes.

    Los timestamps son crecientes para que el orden por sequence_number
    (0..n-1) coincida con el orden cronológico — así el último evento
    devuelto por ``query()`` (ascendente) es el de seq más bajo del cap,
    y el más reciente (seq más alto) queda oculto.
    """
    writer = LedgerWriter(ledger_path)
    for i in range(n_events):
        e = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx-test",
            source="test",
            timestamp=f"2026-06-{(i % 28) + 1:02d}T00:00:00Z",
            payload={"path": payload_path, "action": "create", "idx": i},
        )
        writer.append(e)


# ---------------------------------------------------------------------------
# 1. query sin filtros sobre ledger > cap → setea hint
# ---------------------------------------------------------------------------

def test_query_no_filters_no_limit_sets_hint(tmp_path):
    """Art. III, IX — ``query()`` sin args sobre ledger > cap setea
    ``last_query_hint`` avisando que hay recientes ocultos.

    Anti-teatro: un stub que no implementa el hint falla con AttributeError
    (``last_query_hint`` no existe) o con ``assert is not None``.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)  # 1010 eventos

    index = LedgerIndex(ledger_path)
    results = index.query()  # sin args → cap default

    assert len(results) == DEFAULT_QUERY_LIMIT, (
        f"query() sin limit debe devolver {DEFAULT_QUERY_LIMIT} (cap), "
        f"got {len(results)}"
    )
    # El hint debe estar seteado.
    assert index.last_query_hint is not None, (
        "last_query_hint debe setearse cuando el cap oculta recientes"
    )
    assert index.last_query_hint["hint"] == "result_capped_recents_hidden"
    # 1010 eventos → seq 0..1009 → max_seq = 1009.
    assert index.last_query_hint["max_seq_in_ledger"] == 1009, (
        f"max_seq_in_ledger debe ser 1009 (1010 eventos, seq 0-1009), "
        f"got {index.last_query_hint['max_seq_in_ledger']}"
    )
    # Cap devuelve seq 0..999 → last_seq_returned = 999.
    assert index.last_query_hint["last_seq_returned"] == 999, (
        f"last_seq_returned debe ser 999 (cap devuelve seq 0-999), "
        f"got {index.last_query_hint['last_seq_returned']}"
    )


# ---------------------------------------------------------------------------
# 2. query con from_time/to_time/text/limit explícito → NO hint
# ---------------------------------------------------------------------------

def test_query_with_from_time_does_not_set_hint(tmp_path):
    """Art. IX anti-teatro — caller explícito con ``from_time`` no genera
    hint (ya sabe qué rango quiere, no necesita que le avisemos).

    Anti-teatro: un stub que siempre setea el hint falla este test.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    index = LedgerIndex(ledger_path)
    # from_time que matchea todos los eventos (caller explícito).
    results = index.query(from_time="1900-01-01T00:00:00Z")

    assert index.last_query_hint is None, (
        "from_time explícito no debe setear hint — caller ya filtró"
    )


def test_query_with_to_time_does_not_set_hint(tmp_path):
    """Art. IX anti-teatro — caller explícito con ``to_time`` no genera hint."""
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    index = LedgerIndex(ledger_path)
    results = index.query(to_time="2099-01-01T00:00:00Z")

    assert index.last_query_hint is None, (
        "to_time explícito no debe setear hint — caller ya filtró"
    )


def test_query_with_explicit_limit_no_hint(tmp_path):
    """Art. IX anti-teatro — caller pide ``limit`` explícito → no hint.

    El caller sabe que está pidiendo solo N eventos, no necesita que le
    avisemos que hay más.

    Anti-teatro: un stub que setea el hint cuando len(results) < total
    falla este test (pedimos limit=2 sobre 5 eventos).
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=5)

    index = LedgerIndex(ledger_path)
    results = index.query(limit=2)

    assert len(results) == 2
    assert index.last_query_hint is None, (
        "limit explícito no debe setear hint — caller pidió cap explícito"
    )


def test_query_with_text_does_not_set_hint(tmp_path):
    """Art. IX anti-teatro — caller explorando con ``text`` no genera hint.

    El caller está buscando contenido específico, no pidiendo "lo último".
    No notificamos.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10, payload_path="/x.txt")

    index = LedgerIndex(ledger_path)
    results = index.query(text="/x.txt")

    assert index.last_query_hint is None, (
        "text explícito no debe setear hint — caller está explorando"
    )


# ---------------------------------------------------------------------------
# 3. ledger más chico que el cap → NO hint (anti-teatro fuerte)
# ---------------------------------------------------------------------------

def test_query_ledger_smaller_than_cap_no_hint(tmp_path):
    """Art. IX anti-teatro más fuerte — ledger < cap no setea hint porque
    no hay recientes ocultos (todos los eventos se devuelven).

    Anti-teatro: un stub que setea el hint siempre que len(results) ==
    effective_limit falla este test (5 eventos, cap 1000, no se alcanza).
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=5)

    index = LedgerIndex(ledger_path)
    results = index.query()  # sin args

    assert len(results) == 5
    assert index.last_query_hint is None, (
        "ledger < cap no debe setear hint — no hay recientes ocultos"
    )


# ---------------------------------------------------------------------------
# 4. reset on-entry (no stale state en LedgerIndex compartido)
# ---------------------------------------------------------------------------

def test_query_resets_hint_on_each_call(tmp_path):
    """Art. IX stale state — el hint se resetea al inicio de cada call.

    Escenario: LedgerIndex compartido (ej: MCP server que reusa el mismo
    index para múltiples queries). Primera call setea hint, segunda call
    con limit explícito no debe arrastrar el hint stale.

    Anti-teatro: un stub que no resetea on-entry falla este test porque
    el hint de la primera call persiste en la segunda.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    index = LedgerIndex(ledger_path)
    # Primera call: sin args → hint seteado.
    index.query()
    assert index.last_query_hint is not None, (
        "primera call sin args debe setear hint"
    )
    # Segunda call: con limit explícito → hint debe ser None (reset on-entry).
    index.query(limit=2)
    assert index.last_query_hint is None, (
        "segunda call con limit explícito debe resetear hint a None "
        "(no stale state en LedgerIndex compartido)"
    )


# ---------------------------------------------------------------------------
# 5. Tool MCP propaga el hint en el envelope JSON
# ---------------------------------------------------------------------------

def test_mcp_query_envelope_includes_hint_when_capped(tmp_path):
    """Contrato MCP — ``causadb_query`` incluye las keys ``hint``,
    ``max_seq_in_ledger`` y ``last_seq_returned`` en el envelope cuando
    el cap oculta recientes.

    Anti-teatro: un stub que no propaga el hint falla porque ``"hint"``
    no está en el envelope.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    # event_type explícito pero NO from_time/to_time/text → hint aplica
    # (event_type no cuenta como "filtro de recencia").
    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="FILE_MODIFIED",
        include_payloads=False,  # reducir bytes para evitar byte-cap
    )
    data = json.loads(raw)

    assert "hint" in data, (
        f"envelope debe incluir 'hint' cuando el cap oculta recientes, "
        f"keys={list(data.keys())}"
    )
    assert data["hint"] == "result_capped_recents_hidden"
    assert data["max_seq_in_ledger"] == 1009, (
        f"max_seq_in_ledger debe ser 1009, got {data.get('max_seq_in_ledger')}"
    )
    assert data["last_seq_returned"] == 999, (
        f"last_seq_returned debe ser 999, got {data.get('last_seq_returned')}"
    )


def test_mcp_query_no_hint_when_from_time_set(tmp_path):
    """Anti-teatro — ``causadb_query`` con ``from_time`` no incluye el hint
    en el envelope (caller explícito).

    Anti-teatro: un stub que siempre incluye el hint falla este test.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    raw = causadb_query(
        ledger_path=ledger_path,
        event_type="FILE_MODIFIED",
        from_time="1900-01-01T00:00:00Z",
        include_payloads=False,
    )
    data = json.loads(raw)

    assert "hint" not in data, (
        f"from_time explícito no debe incluir hint en envelope, "
        f"keys={list(data.keys())}"
    )
    assert "max_seq_in_ledger" not in data
    assert "last_seq_returned" not in data


# ---------------------------------------------------------------------------
# 6. CLI propaga --limit y emite hint a stderr
# ---------------------------------------------------------------------------

def test_cli_query_propagates_limit_flag(tmp_path, capsys):
    """Contrato CLI — ``causadb query --limit N`` respeta el limit y
    devuelve exactamente N eventos.

    Anti-teatro: un stub que ignora ``--limit`` devuelve todos los
    eventos y falla la aserción de longitud.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=5)

    args = Namespace(
        ledger=ledger_path,
        event_type=None,
        ctx_id=None,
        parent_event_id=None,
        source=None,
        text=None,
        from_time=None,
        to_time=None,
        limit=2,
    )
    rc, out = cmd_query(args)
    assert rc == 0
    results = json.loads(out)
    assert len(results) == 2, (
        f"--limit 2 debe devolver 2 eventos, got {len(results)}"
    )


def test_cli_query_prints_hint_to_stderr_when_capped(tmp_path, capsys):
    """Contrato CLI — cuando el cap oculta recientes, ``cmd_query`` imprime
    el hint a stderr (stdout sigue siendo el array JSON intacto).

    Anti-teatro: un stub que no imprime el hint falla porque stderr está
    vacío. Un stub que muta stdout falla porque el JSON no parsea.
    """
    ledger_path = str(tmp_path / "ledger.log")
    _seed_ledger(ledger_path, n_events=DEFAULT_QUERY_LIMIT + 10)

    args = Namespace(
        ledger=ledger_path,
        event_type=None,
        ctx_id=None,
        parent_event_id=None,
        source=None,
        text=None,
        from_time=None,
        to_time=None,
        limit=None,  # sin limit → cap default → hint
    )
    rc, out = cmd_query(args)
    captured = capsys.readouterr()
    assert rc == 0

    # stdout debe ser un array JSON válido (no mutado por el hint).
    results = json.loads(out)
    assert isinstance(results, list)
    assert len(results) == DEFAULT_QUERY_LIMIT

    # stderr debe contener el hint.
    assert "result_capped_recents_hidden" in captured.err, (
        f"stderr debe contener 'result_capped_recents_hidden', "
        f"got stderr={captured.err!r}"
    )
    # El hint debe mencionar --from-time o --limit como salida.
    assert "--from-time" in captured.err or "--limit" in captured.err, (
        f"stderr debe sugerir --from-time o --limit, got {captured.err!r}"
    )
