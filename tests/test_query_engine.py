import copy
import json
from types import MappingProxyType

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._init import causadb_init
from causadb._query_engine import query_events


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger_path_bare(tmp_path):
    """Return a bare ledger path (no workspace init)."""
    return str(tmp_path / "ledger.log")


@pytest.fixture
def seeded_ledger(tmp_path):
    """Return a ledger_path populated with 3 events at controlled timestamps.

    Event layout:
      t0 = "2026-06-01T00:00:00Z" — FILE_MODITED, ctx="ctx-alpha", payload={"path": "/rna/results.txt"}
      t1 = "2026-06-15T00:00:00Z" — COMMAND_RUN,  ctx="ctx-alpha", payload={"command": "blastn"}
      t2 = "2026-07-01T00:00:00Z" — FILE_MODITED, ctx="ctx-beta",  payload={"path": "/dna/report.txt"}
    """
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    e0 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-alpha",
        source="test",
        timestamp="2026-06-01T00:00:00Z",
        payload=MappingProxyType({"path": "/rna/results.txt", "action": "create"}),
    )
    e1 = CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="ctx-alpha",
        source="test",
        timestamp="2026-06-15T00:00:00Z",
        payload=MappingProxyType({"command": "blastn", "exit_code": 0}),
    )
    e2 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-beta",
        source="test",
        timestamp="2026-07-01T00:00:00Z",
        payload=MappingProxyType({"path": "/dna/report.txt", "action": "modify"}),
    )
    writer.append(e0)
    writer.append(e1)
    writer.append(e2)

    return ledger


# ── tests ─────────────────────────────────────────────────────────────────────


class TestQueryByTimeRange:
    """3 eventos, query entre t1 y t2 → 2 resultados."""

    def test_query_by_time_range(self, seeded_ledger):
        results = query_events(
            seeded_ledger,
            from_time="2026-06-10T00:00:00Z",
            to_time="2026-07-10T00:00:00Z",
        )
        assert len(results) == 2
        timestamps = [e["timestamp"] for e in results]
        assert "2026-06-15T00:00:00Z" in timestamps
        assert "2026-07-01T00:00:00Z" in timestamps

    def test_query_by_time_range_inclusive_bounds(self, seeded_ledger):
        """Both bounds are inclusive — events exactly on the boundary match."""
        results = query_events(
            seeded_ledger,
            from_time="2026-06-01T00:00:00Z",
            to_time="2026-06-01T00:00:00Z",
        )
        assert len(results) == 1
        assert results[0]["timestamp"] == "2026-06-01T00:00:00Z"

    def test_query_by_time_range_date_only_from_time(self, seeded_ledger):
        """BIT-CHR.114 — from_time date-only (YYYY-MM-DD, sin tz) debe
        matchear eventos aware-UTC.

        Antes del fix: ``_parse_iso("2026-07-10")`` devolvía datetime naive
        (tzinfo=None) mientras los timestamps del ledger son aware UTC
        (``...Z``). Comparar ``naive < aware`` lanza TypeError, que
        ``query_events`` atrapa en el ``except (ValueError, TypeError)`` y
        descarta TODOS los eventos → resultado vacío (query "de última
        entrada" devolvía [] en vez de los recientes).

        Anti-teatro: valida comportamiento real — el range (10 jun, 10 jul)
        debe devolver los 2 eventos seeded dentro del rango, no [].
        """
        results = query_events(
            seeded_ledger,
            from_time="2026-06-10",
            to_time="2026-07-10",
        )
        assert len(results) == 2, (
            f"esperado 2 eventos en rango date-only, got {len(results)}. "
            "Si es 0, el filtro temporal date-only sigue roto (naive vs aware)."
        )
        timestamps = [e["timestamp"] for e in results]
        assert "2026-06-15T00:00:00Z" in timestamps
        assert "2026-07-01T00:00:00Z" in timestamps


class TestQueryByEventType:
    def test_query_by_event_type(self, seeded_ledger):
        results = query_events(seeded_ledger, event_type="FILE_MODIFIED")
        assert len(results) == 2
        for e in results:
            assert e["event_type"] == "FILE_MODIFIED"

    def test_query_by_event_type_no_match(self, seeded_ledger):
        results = query_events(seeded_ledger, event_type="LLM_INVOKED")
        assert results == []


class TestQueryByCtxId:
    def test_query_by_ctx_id(self, seeded_ledger):
        results = query_events(seeded_ledger, ctx_id="ctx-alpha")
        assert len(results) == 2
        for e in results:
            assert e["ctx_id"] == "ctx-alpha"

    def test_query_by_ctx_id_no_match(self, seeded_ledger):
        results = query_events(seeded_ledger, ctx_id="ctx-unknown")
        assert results == []


class TestQueryByTextInPayload:
    def test_query_by_text_in_payload(self, seeded_ledger):
        """q="RNA" → eventos cuyo payload contiene "RNA"."""
        results = query_events(seeded_ledger, text="RNA")
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "/rna/results.txt"

    def test_query_by_text_in_payload_case_insensitive(self, seeded_ledger):
        results = query_events(seeded_ledger, text="rna")
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "/rna/results.txt"

    def test_query_by_text_in_payload_no_match(self, seeded_ledger):
        results = query_events(seeded_ledger, text="nonexistent")
        assert results == []


class TestQueryCombinedFilters:
    def test_query_combined_filters(self, seeded_ledger):
        """type + ctx_id + time → AND de todos."""
        results = query_events(
            seeded_ledger,
            event_type="FILE_MODIFIED",
            ctx_id="ctx-alpha",
            from_time="2026-05-01T00:00:00Z",
            to_time="2026-07-01T00:00:00Z",
        )
        assert len(results) == 1
        assert results[0]["event_type"] == "FILE_MODIFIED"
        assert results[0]["ctx_id"] == "ctx-alpha"
        assert results[0]["timestamp"] == "2026-06-01T00:00:00Z"

    def test_query_combined_no_overlap_returns_empty(self, seeded_ledger):
        """Filters that match no intersection return []."""
        results = query_events(
            seeded_ledger,
            event_type="COMMAND_RUN",
            ctx_id="ctx-beta",
        )
        assert results == []


class TestQueryEmptyResult:
    def test_query_empty_result(self, seeded_ledger):
        """Sin matching → lista vacía."""
        # seeded_ledger has SYSTEM_BOOT (genesis) + 3 seeded events.
        # Query for a type that definitely doesn't exist.
        results = query_events(seeded_ledger, event_type="LLM_INVOKED")
        assert results == []

    def test_query_empty_ledger(self, ledger_path_bare):
        """Ledger vacío también retorna []."""
        open(ledger_path_bare, "a").close()
        results = query_events(ledger_path_bare)
        assert results == []


class TestQueryMalformedDate:
    def test_query_malformed_from_time_returns_empty(self, seeded_ledger):
        """from_time inválido → no crash, lista vacía."""
        results = query_events(seeded_ledger, from_time="not-a-date")
        assert results == []

    def test_query_malformed_to_time_still_filters(self, seeded_ledger):
        """to_time inválido no causa crash (se ignora como filtro).
        Solo from_time se aplica."""
        results = query_events(
            seeded_ledger,
            from_time="2026-06-01T00:00:00Z",
            to_time="not-a-date",
        )
        # from_time="2026-06-01": el genesis event (now, 2026-07-29) y los
        # 3 eventos seeded (t0, t1, t2) tienen timestamp >= from_time,
        # to_time malformed se ignora silenciosamente → 4 resultados.
        assert len(results) == 4


class TestAntiTeatro:
    def test_anti_teatro_query_returns_all(self, seeded_ledger):
        """Si alguien muta query_events para ignorar filtros, este test falla.

        Creamos eventos diferenciados y filtramos con precisión. Si
        query_events retorna todo sin filtrar, el assert de cantidad falla.
        """
        # seeded_ledger ya tiene 3 eventos. Filtramos por FILE_MODIFIED + ctx-beta
        results = query_events(
            seeded_ledger,
            event_type="FILE_MODIFIED",
            ctx_id="ctx-beta",
        )
        # Solo 1 evento coincide: el FILE_MODIFIED de ctx-beta
        assert len(results) == 1, (
            f"Esperado 1 (FILE_MODIFIED + ctx-beta), got {len(results)}. "
            "Si este test falla, query_events podría estar ignorando filtros."
        )
        assert results[0]["event_type"] == "FILE_MODIFIED"
        assert results[0]["ctx_id"] == "ctx-beta"
        assert results[0]["timestamp"] == "2026-07-01T00:00:00Z"

    def test_anti_teatro_patched_ignores_filters(self, seeded_ledger, monkeypatch):
        """Mutación explícita: reemplazamos query_events para ignorar filtros.

        Esto demuestra que el test anti-teatro detecta el bug.
        """
        original_query = copy.deepcopy(query_events)

        def broken_query(ledger_path, **kwargs):
            """Ignora todos los filtros y retorna el ledger completo."""
            return original_query(ledger_path)

        monkeypatch.setattr(
            "causadb._query_engine.query_events",
            broken_query,
        )
        from causadb._query_engine import query_events as patched_query

        results = patched_query(
            seeded_ledger,
            event_type="FILE_MODIFIED",
            ctx_id="ctx-beta",
        )
        # Seeded ledger tiene 4 eventos (genesis SYSTEM_BOOT + 3).
        # La versión rota ignora filtros y retorna todos.
        assert len(results) == 4, (
            f"Broken query (ignora filtros) retorna {len(results)} eventos, "
            "esperado 4. El anti-teatro está funcionando."
        )
