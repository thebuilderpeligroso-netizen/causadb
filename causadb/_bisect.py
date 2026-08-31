"""F.12.5 — `causadb bisect --test cmd`.

Binary search over the event chain to find the first action that broke
the test command. Equivalent to ``git bisect`` but over agent actions
recorded in the CausaDB ledger.

Algorithm (from ``CAUSADB_ROADMAP_FASE12_PLAN.md`` F.12.5, lines 379-387):

1. Convert the event chain to a linear list, oldest-first.
2. Filter: only events with ``post_snapshot is not None``. If an event
   has no snapshot, raise ``BisectError`` (Fall-Closed — Artículo V).
3. ``lo = 0``, ``hi = len-1``, ``first_bad = None``.
4. For each ``mid``: restore ``event.post_snapshot`` to the workspace,
   run ``test_cmd`` via ``subprocess.run``, read the exit code.
5. Pass (exit 0)? ``lo = mid+1``. Fail? ``first_bad = mid; hi = mid``.
6. At the end: restore the workspace to the state of the "bad" event
   (do NOT leave the workspace in a broken intermediate state).
7. Return the "first bad" event (with prompt, reasoning, agent) or
   ``None`` if all events pass.

Artículo V (Fall-Closed): an event without ``post_snapshot`` raises
``BisectError`` explicitly — no silent skip that could return a wrong
answer.
"""

import subprocess
from typing import Optional, Dict, Any

from causadb._ledger_reader import LedgerReader
from causadb._blob_store import BlobStore
from causadb._config import CausaDBConfig


class BisectError(Exception):
    """Raised when bisect cannot proceed (e.g. an event lacks a snapshot)."""


def bisect(test_cmd: str, ledger_path: str, watch_dir: str) -> Optional[Dict[str, Any]]:
    """Binary-search the event chain for the first event whose post-state
    fails *test_cmd*.

    Args:
        test_cmd: Shell command to run against the restored workspace.
            Exit code 0 = pass, non-zero = fail.
        ledger_path: Absolute path to the CausaDB ledger.
        watch_dir: Workspace directory to restore snapshots into. Used as
            the ``cwd`` for *test_cmd*.

    Returns:
        A dict describing the first bad event::

            {
                "event_id": str,
                "post_snapshot": str,
                "prompt": str | None,
                "reasoning": str | None,
                "agent": str,
            }

        Or ``None`` if every event's post-state passes *test_cmd*.

    Raises:
        BisectError: if any event in the chain lacks ``post_snapshot``
            (Fall-Closed — no silent skip).
    """
    from causadb._snapshot import WorkspaceSnapshot

    config = CausaDBConfig(ledger_path=ledger_path)
    store = BlobStore(config.blob_store_path)

    events = list(LedgerReader(ledger_path).read_all())

    snap_events = []
    for ev in events:
        if ev.post_snapshot is None:
            raise BisectError(
                f"event {ev.event_id} has no post_snapshot — "
                f"cannot bisect without a restorable state"
            )
        snap_events.append(ev)

    if not snap_events:
        return None

    lo, hi = 0, len(snap_events) - 1
    first_bad = None
    while lo <= hi:
        mid = (lo + hi) // 2
        ev = snap_events[mid]
        WorkspaceSnapshot.restore(ev.post_snapshot, store, watch_dir)
        proc = subprocess.run(test_cmd, shell=True, cwd=watch_dir)
        if proc.returncode == 0:
            lo = mid + 1
        else:
            first_bad = ev
            hi = mid - 1

    if first_bad is None:
        return None

    WorkspaceSnapshot.restore(first_bad.post_snapshot, store, watch_dir)

    payload = dict(first_bad.payload) if first_bad.payload else {}
    return {
        "event_id": first_bad.event_id,
        "post_snapshot": first_bad.post_snapshot,
        "prompt": payload.get("prompt"),
        "reasoning": payload.get("reasoning"),
        "agent": first_bad.source,
    }
