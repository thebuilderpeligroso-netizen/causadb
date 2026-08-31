"""Benchmarks for revive performance.

These tests verify that revive completes within acceptable time limits.
Run with: pytest causadb/benchmarks/ -v --benchmark-only
"""

import time
import pytest

from causadb.cli._cmd_revive import _run_revive


# Use a real ledger path for integration benchmark
# This test requires a real ledger to be present
LEDGER_REAL = "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/.causadb/ledger.log"


@pytest.mark.benchmark
@pytest.mark.skipif(
    not __import__("os").path.exists(LEDGER_REAL),
    reason="Real ledger not found at LEDGER_REAL path"
)
def test_t4_revive_real_ledger_under_20s():
    """Integración: ``_run_revive`` markdown contra LEDGER_REAL en <20s.

    Baseline pre-P4: ~51s (replay doble + load_skills + 2 lecturas de
    score). Esperado post-P4: ~11-12s frío (1 replay + 1 lectura cruda
    compartida churn/waste). Umbral 20s deja margen para cold-cache.
    """
    start = time.time()
    exit_code, output = _run_revive(
        LEDGER_REAL, output_format="markdown", max_decisions=10
    )
    elapsed = time.time() - start

    assert exit_code == 0, f"revive falló: {output[:500]!r}"
    assert elapsed < 20.0, (
        f"_run_revive tardó {elapsed:.1f}s (baseline ~51s, objetivo <20s)"
    )