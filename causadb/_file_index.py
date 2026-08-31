"""Persistent file index for `causadb why` (BIT-CHR.115 residual debt #1).

Maps normalized file keys to the events whose POST snapshot contains the
file, so ``attribute_line`` is an O(1) lookup plus resolving only the
candidate events' snapshots — instead of a full ledger read with blob
resolution (~163K events, ~119MB, ~3s on the real ledger).

The index is persisted next to the ledger as ``file_index.json`` with a
content hash (``index_hash``) and a schema version. Freshness is checked
via ``last_hash.json`` + first-line identity + size; new events are
tail-extended incrementally without a full rebuild.

Only TOP-LEVEL ``pre_snapshot`` / ``post_snapshot`` fields are indexed —
the payload is NEVER consulted for snapshot detection (top-level-only
gate, Artículo IX anti-teatro: payload-only snapshot references must not
create index keys).
"""
import fcntl
import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from causadb._blob_store import BlobStore
from causadb._ledger_reader import LedgerReader

FILE_INDEX_SCHEMA_VERSION = 1


def _normalize_rel_path(p: str) -> str:
    """Collapse the double ``causadb/causadb/`` prefix to ``causadb/``.

    BIT-CHR.110 TD-#3d: real snapshots store the same file with mixed
    styles (``causadb/causadb/_assistant.py`` vs ``causadb/_assistant.py``).
    This heuristic normalizes the double-prefixed form to the single one;
    any other path is returned unchanged. Deliberately NARROW (no
    suffix-match, YAGNI verified): ``config.py`` must NOT resolve to
    ``causadb/config.py``.
    """
    if p.startswith("causadb/causadb/"):
        return p[len("causadb/"):]
    return p


def _file_index_path_for(ledger_path: str) -> str:
    """The index lives in the SAME directory as the ledger."""
    return os.path.join(
        os.path.dirname(os.path.abspath(ledger_path)), "file_index.json"
    )


def _compute_index_hash(index: Dict[str, Any]) -> str:
    """sha256 of the index JSON, excluding the ``index_hash`` field itself."""
    payload = {k: v for k, v in index.items() if k != "index_hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()


def _save_file_index(index: Dict[str, Any], path: str) -> None:
    """Persist *index* to *path* under an exclusive lock (``path + ".lock"``).

    The ``index_hash`` field is computed and included in the written JSON.
    The directory is created if missing; the file is fsync'd before the
    lock is released (finally LOCK_UN).
    """
    index["index_hash"] = _compute_index_hash(index)
    lock_path = path + ".lock"
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "w") as f:
                json.dump(index, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _load_file_index(path: str) -> Optional[Dict[str, Any]]:
    """Load and verify *path* under a shared lock.

    Returns ``None`` on ANY problem: missing file, JSON decode error,
    schema version mismatch, ``index_hash`` mismatch, or OSError.
    """
    if not os.path.exists(path):
        return None
    lock_path = path + ".lock"
    try:
        with open(lock_path, "a") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
            try:
                with open(path) as f:
                    index = json.load(f)
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        if index.get("schema_version") != FILE_INDEX_SCHEMA_VERSION:
            return None
        if index.get("index_hash") != _compute_index_hash(index):
            return None
        return index
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _read_last_hash(ledger_path: str) -> Optional[str]:
    """Read ``<ledger>.last_hash.json`` → ``{"last_hash": "<sha256>"}``.

    Returns ``None`` if the file is missing or unreadable.
    """
    last_hash_path = ledger_path + ".last_hash.json"
    if not os.path.exists(last_hash_path):
        return None
    try:
        with open(last_hash_path) as f:
            return json.load(f).get("last_hash")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _snapshot_files(snap_hash: Optional[str], store: BlobStore) -> set:
    """Normalized set of file keys in snapshot *snap_hash*.

    ``None`` hash or a missing blob → empty set (fault isolation: a
    missing snapshot blob must not break the whole index build).
    """
    if snap_hash is None:
        return set()
    try:
        snap = store.get(snap_hash)
    except FileNotFoundError:
        return set()
    return {_normalize_rel_path(k) for k in snap.get("files", {}).keys()}


def _index_entry(entry: Dict[str, Any], files: Dict[str, Any], store: BlobStore) -> None:
    """Fold one ledger entry into the *files* index dict.

    Only TOP-LEVEL ``pre_snapshot`` / ``post_snapshot`` fields are
    consulted — the payload is NEVER resolved for snapshot detection
    (top-level-only gate). Pre-side keys create the entry (gate) with an
    empty post list; post-side keys append a record in ledger order,
    deduped by ``(event_id, norm_key)``. Payloads are stored RAW
    (unresolved) — only the introducer's payload is resolved later.
    """
    ev = entry["event"]
    event_id = ev.get("event_id")
    pre_snap_hash = ev.get("pre_snapshot")
    post_snap_hash = ev.get("post_snapshot")

    pre_files = _snapshot_files(pre_snap_hash, store)
    post_files = _snapshot_files(post_snap_hash, store)

    # Pre-side gate: keys present in PRE create the entry (empty post).
    for norm_key in pre_files:
        files.setdefault(norm_key, {"post": []})

    # Post-side records, in ledger order, deduped by (event_id, norm_key).
    for norm_key in post_files:
        post_list = files.setdefault(norm_key, {"post": []})["post"]
        if any(r["event_id"] == event_id for r in post_list):
            continue
        post_list.append({
            "event_id": event_id,
            "pre_snapshot": pre_snap_hash,
            "post_snapshot": post_snap_hash,
            "event_type": str(ev.get("event_type")),
            "source": ev.get("source"),
            "timestamp": ev.get("timestamp"),
            "parent_event_id": ev.get("parent_event_id"),
            "payload": ev.get("payload") or {},
        })


def build_file_index(ledger_path: str, store: BlobStore) -> Dict[str, Any]:
    """Full build: iterate the whole ledger (archives + file) once.

    ``resolve_blobs=False`` — payloads are stored RAW (unresolved) in the
    records; only the introducer's payload is resolved later by
    ``attribute_line`` (fault isolation). If the ledger does not exist, an
    empty index is returned (``first_event_id=""``, ``last_offset=0``,
    ``files={}``).
    """
    files: Dict[str, Any] = {}
    first_event_id = ""

    if os.path.exists(ledger_path):
        reader = LedgerReader(ledger_path)
        for entry in reader.read_all_entries(resolve_blobs=False):
            ev = entry["event"]
            if not first_event_id:
                first_event_id = ev.get("event_id") or ""
            _index_entry(entry, files, store)
        last_offset = os.path.getsize(ledger_path)
    else:
        last_offset = 0

    archive_dir = os.path.join(
        os.path.dirname(os.path.abspath(ledger_path)), "archive"
    )
    if os.path.isdir(archive_dir):
        covered_archives = sorted(os.listdir(archive_dir))
    else:
        covered_archives = []

    return {
        "schema_version": FILE_INDEX_SCHEMA_VERSION,
        "first_event_id": first_event_id,
        "last_offset": last_offset,
        "last_hash": _read_last_hash(ledger_path),
        "covered_archives": covered_archives,
        "files": files,
        "built_at": datetime.utcnow().isoformat() + "Z",
    }


def _extend_file_index(
    index: Dict[str, Any], ledger_path: str, store: BlobStore
) -> Optional[Dict[str, Any]]:
    """Incrementally extend *index* with new ledger lines after ``last_offset``.

    Returns ``None`` when the FIRST new line fails to parse (the ledger
    head is corrupt → force a full rebuild). A torn line AFTER at least
    one successfully parsed line stops the extend WITHOUT advancing
    ``last_offset`` (the torn write is retried on the next call).
    ``first_event_id`` and ``covered_archives`` are never touched.
    """
    if not os.path.exists(ledger_path):
        return index
    try:
        f = open(ledger_path, "rb")
    except OSError:
        return None
    with f:
        f.seek(index.get("last_offset", 0))
        parsed_any = False
        torn = False
        while True:
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if not parsed_any:
                    return None  # first new line corrupt → rebuild
                torn = True
                break
            parsed_any = True
            _index_entry(entry, index["files"], store)
        if not torn:
            index["last_offset"] = os.path.getsize(ledger_path)
        index["last_hash"] = _read_last_hash(ledger_path)
        index["built_at"] = datetime.utcnow().isoformat() + "Z"
    return index


def _first_line_event_id(ledger_path: str) -> Optional[str]:
    """event_id of the FIRST line of the ledger file (None if empty/unreadable)."""
    if not os.path.exists(ledger_path):
        return None
    try:
        with open(ledger_path, "rb") as f:
            line = f.readline()
    except OSError:
        return None
    if not line:
        return None
    try:
        entry = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return entry.get("event", {}).get("event_id")


def get_file_index(ledger_path: str, store: BlobStore) -> Dict[str, Any]:
    """Orchestrator: load-or-build the persistent index, keeping it fresh.

    Freshness pipeline:
      1. Load (or build if missing/corrupt/version-mismatched).
      2. Archive guard: archives changed → rebuild.
      3. ``last_hash.json`` unchanged (or missing — legacy) → fresh.
      4. First-line identity: ledger replaced/truncated at head → rebuild.
      5. Size < ``last_offset`` → truncated → rebuild.
      6. Size > ``last_offset`` → tail-extend (or rebuild if the extend
         detects a corrupt head).
    """
    path = _file_index_path_for(ledger_path)
    idx = _load_file_index(path)

    if idx is None:
        idx = build_file_index(ledger_path, store)
        _save_file_index(idx, path)
        return idx

    # 3. Archive guard.
    archive_dir = os.path.join(
        os.path.dirname(os.path.abspath(ledger_path)), "archive"
    )
    if os.path.isdir(archive_dir):
        current_archives = sorted(os.listdir(archive_dir))
    else:
        current_archives = []
    if current_archives != idx.get("covered_archives", []):
        idx = build_file_index(ledger_path, store)
        _save_file_index(idx, path)
        return idx

    # 4. last_hash freshness (missing last_hash.json → legacy, trust it).
    actual_hash = _read_last_hash(ledger_path)
    if actual_hash is None or actual_hash == idx.get("last_hash"):
        return idx

    # 5. First-line identity.
    first_event_id = _first_line_event_id(ledger_path)
    if first_event_id is None or first_event_id != idx.get("first_event_id"):
        idx = build_file_index(ledger_path, store)
        _save_file_index(idx, path)
        return idx

    # 6. Truncation.
    if os.path.getsize(ledger_path) < idx.get("last_offset", 0):
        idx = build_file_index(ledger_path, store)
        _save_file_index(idx, path)
        return idx

    # 7. Tail-extend.
    if os.path.getsize(ledger_path) > idx.get("last_offset", 0):
        extended = _extend_file_index(idx, ledger_path, store)
        if extended is not None:
            _save_file_index(extended, path)
            return extended
        idx = build_file_index(ledger_path, store)
        _save_file_index(idx, path)
        return idx

    return idx