"""TDD RED: split append() en Fase 1 (fuera del lock) + Fase 2 (bajo lock_ex).

Specs auditadas:
- Fase 1 FUERA del lock = snapshots + BlobStore (timeout 5s existente).
- Fase 2 BAJO lock_ex = SOLO _get_last_hash + _get_next_sequence_number
  + write+fsync + sidecar.
- NO invertir (snapshot despues romperia pre/post).
- No tocar _get_last_hash/sidecar (ya atomicos BIT-CHR.144).
"""
import json
import threading
import time
from unittest import mock

from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._config import CausaDBConfig


def _make_writer(ledger_path, workspace_dir=None):
    cfg = CausaDBConfig(
        ledger_path=ledger_path,
        redaction_enabled=False,
        blob_store_enabled=False,
        workspace_dir=workspace_dir,
    )
    return LedgerWriter(ledger_path, cfg)


def _writes_event():
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="opencode:agent",
        payload={"writes": [{"path": "a.txt"}]},
    )


def test_concurrent_appends_do_not_serialize_on_snapshot(tmp_path):
    """Dos appends con snapshot lento (2s c/u) deben solaparse fuera del lock.

    Codigo viejo (snapshot BAJO lock): serializa -> ~8s (pre 2s + post 2s,
    dos appends en serie).
    Codigo nuevo (Fase 1 fuera del lock): paralelo -> ~4s.
    (Extrapola al timeout real 5s: un append bloquea al otro ~10s
    pre 5s + post 5s bajo lock; fuera del lock se solapan.)
    """
    ledger_path = str(tmp_path / "ledger.log")
    ws = str(tmp_path / "ws")
    import os
    os.makedirs(ws, exist_ok=True)
    writer = _make_writer(ledger_path, workspace_dir=ws)

    def slow_snapshot(self, workspace_dir, prev_snapshot=None):
        time.sleep(2)
        return ({"files": {}}, "fakehash123")

    with mock.patch.object(LedgerWriter, "_snapshot_with_timeout", slow_snapshot):
        t0 = time.monotonic()
        errors = []

        def do_append():
            try:
                writer.append(_writes_event())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=do_append) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0

    assert not errors, f"appends fallaron: {errors}"
    with open(ledger_path) as f:
        assert len(f.readlines()) == 2
    # Serial = ~8s; paralelo = ~4s. Umbral 6s distingue ambos con margen.
    assert elapsed < 6.0, (
        f"appends concurrentes se bloquearon {elapsed:.1f}s "
        f"(snapshot bajo lock serializa; esperado <6.0s en paralelo)"
    )


def test_pre_snapshot_taken_outside_lock_before_write(tmp_path):
    """Snapshot debe ocurrir FUERA del lock y ANTES de la linea anexada.

    - Si el snapshot corre bajo self._lock -> falla (debe ser Fase 1 fuera).
    - La entrada anexada debe llevar el pre_snapshot (causalidad: el
      snapshot es anterior a su propia linea; nunca despues).
    """
    ledger_path = str(tmp_path / "ledger.log")
    ws = str(tmp_path / "ws")
    import os
    os.makedirs(ws, exist_ok=True)
    writer = _make_writer(ledger_path, workspace_dir=ws)

    seen_locked = []

    def spy_snapshot(self, workspace_dir, prev_snapshot=None):
        seen_locked.append(self._lock.locked())
        return ({"files": {}}, "prehash-causal")

    with mock.patch.object(LedgerWriter, "_snapshot_with_timeout", spy_snapshot):
        writer.append(_writes_event())

    assert seen_locked, "el snapshot nunca se ejecuto"
    assert seen_locked[0] is False, (
        "snapshot ejecutado BAJO el lock (Fase 1 debe ser FUERA del lock)"
    )
    with open(ledger_path) as f:
        entry = json.loads(f.readline())
    assert entry["event"]["payload"].get("pre_snapshot") == "prehash-causal", (
        "pre_snapshot no llego a la linea anexada (causalidad rota)"
    )
