"""F.12.2 — Causal line attribution (`causadb why file:line`).

Given a file path and a 1-based line number, find the event that
introduced that line — i.e. the line is present in the event's
``post_snapshot`` but NOT in its ``pre_snapshot``. This is the
event-agent equivalent of ``git blame``.

Algorithm (BIT-CHR.110 TD-#3a — no longer walks the ``parent_event_id``
chain, which is unset on ~99.99% of real ledger events):

1. Look up the file in the persistent index (``<ledger_dir>/file_index.json``,
   built by ``causadb._file_index``): the index maps normalized file keys
   to the events whose POST snapshot contains the file, in ledger order.
2. Iterate the candidate records in REVERSE order (newest first):
   - Read the file from the post snapshot → if the line EXISTS there.
   - Read the file from the pre snapshot → if the line does NOT exist there.
   - If line is in post but not in pre → this event introduced it.
3. If no event transitions the line, return the OLDEST event whose post
   contains the line (the "root of snapshots" fallback, TD-#3c).
4. Return the introducer event with ``prompt``, ``reasoning``, ``agent``,
   ``source``, ``timestamp``.

Index freshness (BIT-CHR.115 residual debt #1):
- The index is persisted next to the ledger as ``file_index.json`` with a
  content hash + schema version; it is rebuilt on corruption, version
  mismatch, archive changes, head replacement, or truncation.
- New events are tail-extended incrementally (``last_hash.json`` +
  ``last_offset``), so a query after an append does NOT re-read the whole
  ledger.

Fault isolation (intentional improvement): only the INTRODUCER's payload
is resolved (``$blob`` → materialized dict). A missing blob in a
NON-candidate event no longer breaks attribution — the old full-scan
implementation resolved every event's payload up front.

Edge cases:
- If the line existed since the root of snapshots (no event introduced
  it later), the root event IS the introducer (it first captured the
  line in a snapshot).
- Paths are resolved with ``_resolve_file_key``: exact match first,
  then a narrow normalization that collapses the double
  ``causadb/causadb/`` prefix (TD-#3d).
- If the file was never touched in the ledger, raise ``ValueError`` with
  an informative message mentioning the file.
"""
import base64
import os
from typing import Optional, Dict, Any

from causadb._blob_store import BlobStore, resolve_payload
from causadb._file_index import _normalize_rel_path, get_file_index
from causadb._ledger_reader import LedgerReader


def _blob_store_for(ledger_path: str) -> BlobStore:
    """Return a BlobStore rooted next to the ledger (mirrors CausaDBConfig)."""
    base = os.path.join(os.path.dirname(os.path.abspath(ledger_path)), "blobs")
    return BlobStore(base)


def _resolve_file_key(files: dict, rel_path: str) -> Optional[str]:
    """Resolve *rel_path* to a real key of the snapshot *files* dict.

    1. Exact match first (``rel_path in files``).
    2. Fallback: normalized match — find a key ``k`` such that
       ``_normalize_rel_path(k) == _normalize_rel_path(rel_path)``.

    Centralizes the lookup so ``_file_lines_from_snapshot`` and the index
    key resolution cannot diverge (TD-#3d).

    Returns:
        The real key in *files*, or ``None`` if no match.
    """
    if rel_path in files:
        return rel_path
    norm = _normalize_rel_path(rel_path)
    for k in files:
        if _normalize_rel_path(k) == norm:
            return k
    return None


def _file_lines_from_snapshot(
    snapshot_hash: Optional[str],
    store: BlobStore,
    rel_path: str,
) -> Optional[list]:
    """Return the list of lines of file *rel_path* in snapshot *snapshot_hash*.

    Returns ``None`` if the snapshot is None, the file is absent, or the
    content blob cannot be resolved.
    """
    if snapshot_hash is None:
        return None
    try:
        snap = store.get(snapshot_hash)
    except FileNotFoundError:
        return None
    files = snap.get("files", {})
    file_key = _resolve_file_key(files, rel_path)
    if file_key is None:
        return None
    entry = files[file_key]
    blob_refs = snap.get("blob_refs", {})
    blob_sha = blob_refs.get(entry["hash"])
    if blob_sha is None:
        return None
    try:
        content_blob = store.get(blob_sha)
    except FileNotFoundError:
        return None
    content = base64.b64decode(content_blob["content_b64"])
    return content.decode("utf-8").splitlines()


def _line_exists(lines: Optional[list], line_number: int) -> bool:
    """True if *line_number* (1-based) is within the lines list."""
    if lines is None:
        return False
    return 1 <= line_number <= len(lines)


def _resolve_payload_into(rec: dict, store: BlobStore) -> None:
    """Resolve the record's RAW payload in place (``$blob`` → dict).

    Only the introducer's payload is resolved — a missing blob in a
    NON-candidate event never breaks attribution (fault isolation).
    """
    rec["payload"] = resolve_payload(rec["payload"] or {}, store)


def _introducer_dict(ev) -> Dict[str, Any]:
    """Build the return dict for an introducer event *ev*.

    Accepts an index RECORD (dict) or an object with attributes
    (CanonicalEvent). ``event_type`` may be a plain string (index records)
    or an Enum — ``getattr(et, "value", et)`` normalizes both (a plain
    string has no ``.value`` attribute).

    The dict shape is the public contract of ``attribute_line``:
    ``{"event_id", "event_type", "source", "timestamp", "prompt",
    "reasoning", "agent", "parent_event_id"}``.
    """
    if isinstance(ev, dict):
        et = ev.get("event_type")
        et = getattr(et, "value", et)
        payload = ev.get("payload") or {}
        return {
            "event_id": ev.get("event_id"),
            "event_type": et,
            "source": ev.get("source"),
            "timestamp": ev.get("timestamp"),
            "prompt": payload.get("prompt"),
            "reasoning": payload.get("reasoning"),
            "agent": ev.get("source"),
            "parent_event_id": ev.get("parent_event_id"),
        }
    et = getattr(ev.event_type, "value", ev.event_type)
    return {
        "event_id": ev.event_id,
        "event_type": et,
        "source": ev.source,
        "timestamp": ev.timestamp,
        "prompt": ev.payload.get("prompt"),
        "reasoning": ev.payload.get("reasoning"),
        "agent": ev.source,
        "parent_event_id": ev.parent_event_id,
    }


def attribute_line(file_path: str, line_number: int, ledger_path: str) -> Optional[Dict[str, Any]]:
    """Find the event that introduced line *line_number* of *file_path*.

    Uses the persistent file index (``<ledger_dir>/file_index.json``,
    BIT-CHR.115 residual debt #1): the index maps normalized file keys to
    the events whose POST snapshot contains the file, in ledger order.
    The candidate records are iterated in REVERSE order (newest first)
    and the first event where the line is present in post but absent in
    pre is the introducer.

    The iteration order is the ledger order (preserved by the index
    build), NOT sorted by timestamp: the real ledger mixes ``int`` mtimes
    with ISO strings, which makes timestamp ordering unstable. Ledger
    order is the only total order available.

    The algorithm does NOT depend on the ``parent_event_id`` chain
    (BIT-CHR.110 TD-#3a): the harvester leaves ``parent_event_id``
    unset on ~99.99% of real events, so a chain walk stops at HEAD and
    yields nothing.

    Semantics of ``pre_snapshot is None`` (BIT-CHR.110): an event with
    ``pre_snapshot=None`` and the line present in post IS a candidate
    introducer — ``_file_lines_from_snapshot(None, ...)`` returns None
    and ``_line_exists(None, ...)`` returns False, so the line is
    treated as "not present in pre". This is intentional: we cannot
    prove the line existed before the event, so the event takes credit.

    If no event transitions the line (it existed since the root of
    snapshots — present in pre AND post of every event), the OLDEST
    event whose post contains the line is returned (it first captured
    the line in a snapshot — the "root of snapshots" fallback, TD-#3c).

    Index freshness: the index is rebuilt on corruption, version
    mismatch, archive changes, head replacement, or truncation; new
    events are tail-extended incrementally via ``last_hash.json`` +
    ``last_offset`` (no full re-read after an append).

    Fault isolation (intentional improvement): only the INTRODUCER's
    payload is resolved (``$blob`` → materialized dict). A missing blob
    in a NON-candidate event no longer breaks attribution — the old
    full-scan implementation resolved every event's payload up front.

    Args:
        file_path: relative path of the file within the workspace snapshot
            (e.g. ``"main.py"``, ``"src/utils.py"``).
        line_number: 1-based line number to attribute.
        ledger_path: absolute path to the ledger file.

    Returns:
        A dict with the introducer event's fields:
        ``{"event_id", "event_type", "source", "timestamp", "prompt",
        "reasoning", "agent", "parent_event_id"}``.
        Returns ``None`` if the line is not present in any snapshot.

    Raises:
        ValueError: if *file_path* never appears in any event's snapshot
            (the file was never touched in the ledger).
    """
    store = _blob_store_for(ledger_path)
    index = get_file_index(ledger_path, store)
    norm = _normalize_rel_path(file_path)
    records = index["files"].get(norm)
    if records is None:
        raise ValueError(
            f"File '{file_path}' was never touched in the ledger "
            f"({ledger_path}); cannot attribute a line to an event."
        )

    # Candidate records (events whose POST contains the file), newest
    # first (reverse ledger order). Only these can introduce a line (the
    # line must be present in a post snapshot to be attributable).
    #
    # fallback: the OLDEST event whose post contains the line (the root
    # of snapshots). Reassigned on every record whose post has the line,
    # so at the end it holds the last (oldest) such record (TD-#3c).
    fallback: Optional[Any] = None
    for rec in reversed(records["post"]):
        post_lines = _file_lines_from_snapshot(rec["post_snapshot"], store, file_path)
        if not _line_exists(post_lines, line_number):
            continue
        fallback = rec
        pre_lines = _file_lines_from_snapshot(rec["pre_snapshot"], store, file_path)
        if not _line_exists(pre_lines, line_number):
            _resolve_payload_into(rec, store)
            return _introducer_dict(rec)

    if fallback is not None:
        _resolve_payload_into(fallback, store)
        return _introducer_dict(fallback)
    return None
