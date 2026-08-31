"""Benchmarks for ReplayEngine performance.

These tests verify that replay scales linearly, not quadratically.
Run with: pytest causadb/benchmarks/ -v --benchmark-only
"""

import time
import pytest

from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._replay_engine import ReplayEngine


@pytest.mark.benchmark
def test_replay_scales_linear_not_quadratic(tmp_path):
    """BIT-CHR.34 — Anti-regresión: reconstruir un ledger sintético grande
    completa en < 2s (el deepcopy por evento lo llevaba a > 4s en 2000
    eventos = O(n²)).

    Solo cronometra reconstruct_state(), NO la construcción del ledger
    (los writer.append() hacen fsync y tardan por sí solos).
    """
    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)
    for i in range(2000):
        writer.append(CanonicalEvent(
            event_type=EventType.COMMAND_RUN,
            ctx_id="ctx",
            source="causadb:test",
            payload={"command": f"cmd {i}"},
        ))

    engine = ReplayEngine(ledger)
    t0 = time.monotonic()
    state = engine.reconstruct_state()
    elapsed = time.monotonic() - t0

    assert state["events_applied"] == 2000
    assert elapsed < 2.0, f"reconstruct_state tardó {elapsed:.2f}s para 2000 eventos (O(n²)?)"