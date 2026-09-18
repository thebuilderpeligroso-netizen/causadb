"""Fase 4 B-01 — Corte de luz + links (RED).

- Media línea sin ``\\n`` → próximo append válido + 1 ``.corrupt-*``
  + ningún GENESIS duplicado (prev_hash encadena, no resetea).
- Cadena rota-parseable → CorruptionError (fail-closed, jamás GENESIS/seq-0).
"""
import glob
import json
import os

import pytest

from causadb._ledger_writer import LedgerWriter, CorruptionError
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


def _mk_event():
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="agent"
    )


def test_truncated_tail_quarantined_and_next_append_valid(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    w = LedgerWriter(ledger)
    e1 = _mk_event()
    entry1 = w.append(e1)
    h1 = entry1["hash"]

    # Simular corte de luz: media línea sin "\n" anexada.
    with open(ledger, "ab") as f:
        f.write(b'{"event": {"seq')

    w2 = LedgerWriter(ledger)
    e2 = _mk_event()
    entry2 = w2.append(e2)

    # 1 .corrupt-* generado.
    corrupts = glob.glob(ledger + ".corrupt-*")
    assert len(corrupts) == 1, f"se esperaba 1 .corrupt-*, got {corrupts}"

    # Ningún GENESIS duplicado: el nuevo evento encadena con h1.
    assert entry2["prev_hash"] == h1, (
        f"prev_hash debe encadenar con h1, got {entry2['prev_hash']!r}"
    )

    # Ledger final: 2 líneas válidas parseables.
    with open(ledger, "r") as f:
        lines = [ln for ln in f.readlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)


def test_broken_chain_parseable_raises_corruption(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    w = LedgerWriter(ledger)
    w.append(_mk_event())

    # Línea parseable pero con cadena rota (prev_hash/hash forjados).
    forged = {"event": {"sequence_number": 999}, "prev_hash": "FORGED", "hash": "FORGED"}
    with open(ledger, "a") as f:
        f.write(json.dumps(forged, sort_keys=True) + "\n")

    w2 = None
    with pytest.raises(CorruptionError):
        # Fail-closed: salta ya en construcción (_get_last_hash) o en
        # append — jamás GENESIS/seq-0 silencioso.
        w2 = LedgerWriter(ledger)
        w2.append(_mk_event())

    # Fail-closed: el ledger corrupto NO fue truncado ni reseteado.
    with open(ledger, "r") as f:
        lines = f.readlines()
    assert len(lines) == 2
