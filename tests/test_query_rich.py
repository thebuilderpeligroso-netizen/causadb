"""P1.1 — Rich Query API: 6 tests for new filters (parent_event_id, source, text, time)
across both query_events() and LedgerIndex.query().
"""

import json
from types import MappingProxyType

import pytest

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._init import causadb_init
from causadb._query_engine import query_events
from causadb._ledger_index import LedgerIndex


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_ledger(tmp_path):
    """Return a ledger_path populated with 4 events at controlled timestamps.

    Event layout:
      t0 = "2026-06-01T00:00:00Z" — FILE_MODIFIED, ctx="ctx-alpha",
           source="harvester:opencode", payload={"path": "/rna/causadb_results.txt"}
      t1 = "2026-06-15T00:00:00Z" — COMMAND_RUN,  ctx="ctx-alpha",
           source="harvester:opencode", payload={"command": "blastn"}
      t2 = "2026-07-01T00:00:00Z" — FILE_MODIFIED, ctx="ctx-beta",
           source="causadb:vigilante", payload={"path": "/dna/report.txt"}
      t3 = "2026-07-15T00:00:00Z" — FILE_MODIFIED, ctx="ctx-alpha",
           source="harvester:opencode", payload={"path": "/rna/seq.txt"},
           parent_event_id = t0.event_id
    """
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)

    e0 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-alpha",
        source="harvester:opencode",
        timestamp="2026-06-01T00:00:00Z",
        payload=MappingProxyType({"path": "/rna/causadb_results.txt", "action": "create"}),
    )
    e1 = CanonicalEvent(
        event_type=EventType.COMMAND_RUN,
        ctx_id="ctx-alpha",
        source="harvester:opencode",
        timestamp="2026-06-15T00:00:00Z",
        payload=MappingProxyType({"command": "blastn", "exit_code": 0}),
    )
    e2 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-beta",
        source="causadb:vigilante",
        timestamp="2026-07-01T00:00:00Z",
        payload=MappingProxyType({"path": "/dna/report.txt", "action": "modify"}),
    )
    e3 = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx-alpha",
        source="harvester:opencode",
        timestamp="2026-07-15T00:00:00Z",
        payload=MappingProxyType({"path": "/rna/seq.txt", "action": "create"}),
        parent_event_id=e0.event_id,
    )
    writer.append(e0)
    writer.append(e1)
    writer.append(e2)
    writer.append(e3)

    return ledger, e0, e1, e2, e3


# ── Test 1: text filter (case-insensitive) ────────────────────────────────────


class TestTextFilter:
    def test_text_filter_case_insensitive(self, seeded_ledger):
        """query_events(..., text="causadb") matches payloads containing 'causadb'."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, text="causadb")
        # Only e0 has "causadb" in its payload path
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "/rna/causadb_results.txt"

    def test_text_filter_no_match(self, seeded_ledger):
        """query_events(..., text="nonexistent") returns []."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, text="nonexistent")
        assert results == []


# ── Test 2: from_time / to_time range ─────────────────────────────────────────


class TestTimeRange:
    def test_from_time_inclusive(self, seeded_ledger):
        """Events at from_time boundary are included."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, from_time="2026-06-01T00:00:00Z")
        # All 4 seeded events + genesis SYSTEM_BOOT are >= 2026-06-01
        seeded = [e for e in results if e["event_type"] != "SYSTEM_BOOT"]
        assert len(seeded) == 4

    def test_to_time_inclusive(self, seeded_ledger):
        """Events at to_time boundary are included."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, to_time="2026-06-01T00:00:00Z")
        # Only e0 (2026-06-01) matches; genesis is at init time (now) > 2026-06-01
        assert len(results) == 1
        assert results[0]["timestamp"] == "2026-06-01T00:00:00Z"

    def test_from_time_exclusive_before(self, seeded_ledger):
        """Events before from_time are excluded."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, from_time="2026-07-10T00:00:00Z")
        # e3 (2026-07-15) matches, plus genesis SYSTEM_BOOT (timestamp=now)
        # which is also >= 2026-07-10. Filter to only seeded events.
        seeded = [e for e in results if e["event_type"] != "SYSTEM_BOOT"]
        assert len(seeded) == 1
        assert seeded[0]["timestamp"] == "2026-07-15T00:00:00Z"

    def test_to_time_exclusive_after(self, seeded_ledger):
        """Events after to_time are excluded."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, to_time="2026-06-10T00:00:00Z")
        # Only e0 (2026-06-01) is before 2026-06-10; genesis is now > 2026-06-10
        assert len(results) == 1
        assert results[0]["timestamp"] == "2026-06-01T00:00:00Z"


# ── Test 3: parent_event_id filter ────────────────────────────────────────────


class TestParentEventId:
    def test_parent_event_id_exact_match(self, seeded_ledger):
        """query_events(..., parent_event_id=e0.event_id) returns e3 only."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, parent_event_id=e0.event_id)
        assert len(results) == 1
        assert results[0]["event_id"] == e3.event_id
        assert results[0]["parent_event_id"] == e0.event_id

    def test_parent_event_id_no_match(self, seeded_ledger):
        """query_events(..., parent_event_id="nonexistent") returns []."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, parent_event_id="nonexistent-id")
        assert results == []


# ── Test 4: source filter ─────────────────────────────────────────────────────


class TestSourceFilter:
    def test_source_exact_match(self, seeded_ledger):
        """query_events(..., source="harvester:opencode") returns 3 events."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, source="harvester:opencode")
        # e0, e1, e3 have source="harvester:opencode"
        assert len(results) == 3
        for e in results:
            assert e["source"] == "harvester:opencode"

    def test_source_no_match(self, seeded_ledger):
        """query_events(..., source="nonexistent:source") returns []."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(ledger, source="nonexistent:source")
        assert results == []


# ── Test 5: ALL 3 new filters combined (AND) ──────────────────────────────────


class TestCombinedNewFilters:
    def test_all_new_filters_combined(self, seeded_ledger):
        """text + time + parent_event_id AND-combined."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(
            ledger,
            text="seq",
            from_time="2026-07-01T00:00:00Z",
            to_time="2026-08-01T00:00:00Z",
            parent_event_id=e0.event_id,
        )
        # Only e3 matches all: has "seq" in payload, timestamp 2026-07-15,
        # and parent_event_id = e0.event_id
        assert len(results) == 1
        assert results[0]["event_id"] == e3.event_id
        assert results[0]["parent_event_id"] == e0.event_id

    def test_new_filters_no_overlap(self, seeded_ledger):
        """Filters that match no intersection return []."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        results = query_events(
            ledger,
            text="causadb",
            source="causadb:vigilante",
        )
        # e0 has "causadb" but source is "harvester:opencode"
        # e2 has source "causadb:vigilante" but no "causadb" in payload
        assert results == []


# ── Test 6: LedgerIndex.query() delegates to query_events() for text/time ─────


class TestLedgerIndexRichQuery:
    def test_ledger_index_with_text_delegates_to_query_events(self, seeded_ledger):
        """LedgerIndex.query(text="causadb") delegates to query_events and filters correctly."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        index = LedgerIndex(ledger)
        results = index.query(text="causadb")
        assert len(results) == 1
        # LedgerIndex.query returns full entries (event + hash + prev_hash)
        event = results[0]["event"]
        assert event["payload"]["path"] == "/rna/causadb_results.txt"

    def test_ledger_index_with_from_time_delegates_to_ledger(self, seeded_ledger):
        """LedgerIndex.query() with from_time filters correctly."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        index = LedgerIndex(ledger)
        results = index.query(from_time="2026-07-10T00:00:00Z")
        # e3 (2026-07-15) matches, plus genesis SYSTEM_BOOT (timestamp=now)
        seeded = [r for r in results if r["event"]["event_type"] != "SYSTEM_BOOT"]
        assert len(seeded) == 1
        assert seeded[0]["event"]["timestamp"] == "2026-07-15T00:00:00Z"

    def test_ledger_index_with_combined_index_and_text(self, seeded_ledger):
        """LedgerIndex.query() with event_type (index) + text (fallback) works."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        index = LedgerIndex(ledger)
        results = index.query(event_type="FILE_MODIFIED", text="dna")
        # Only e2: FILE_MODIFIED + "dna" in payload
        assert len(results) == 1
        assert results[0]["event"]["payload"]["path"] == "/dna/report.txt"

    def test_ledger_index_no_text_time_uses_index(self, seeded_ledger):
        """LedgerIndex.query() without text/time uses the index (fast path)."""
        ledger, e0, e1, e2, e3 = seeded_ledger
        index = LedgerIndex(ledger)
        results = index.query(event_type="FILE_MODIFIED")
        # e0, e2, e3 are FILE_MODIFIED
        assert len(results) == 3
        for entry in results:
            assert entry["event"]["event_type"] == "FILE_MODIFIED"