"""GC de blobs huerfanos (BIT-CHR.35 P4).

Un blob huerfano es un blob en disco que NO tiene ninguna referencia
``$blob`` en el ledger (ni en payloads de eventos, ni en
``pre_snapshot``/``post_snapshot``). El GC los identifica y, en modo
``execute``, los mueve a ``blobs/.trash/<shard>/<hash>.json`` via
``os.rename`` (atomico en mismo filesystem). Nunca borra definitivamente
— ``.trash/`` es recoverable.

Clases reconocidas (desglose ``by_class``):
- ``payload``: blobs referenciados via ``$blob`` en el ``payload``.
- ``snapshot``: blobs referenciados via ``pre_snapshot`` o ``post_snapshot``.
- ``harvest``: blobs escritos por ``FilesystemSource._maybe_capture_content``.
  Identificacion estructural por SUBSET de las 4 claves requeridas
  ``{"path", "content", "size", "mtime"}`` (tolerante a claves extra como
  ``action`` y ``content_hash`` introducidas por FIX.5). La coincidencia
  exacta de 4 claves rompia la exclusion de harvest-class post-FIX.5.
- ``orphan``: blobs en disco sin ref en ninguna clase anterior.

Modos:
- ``dry_run=True`` (default): reporta, NO muta.
- ``dry_run=False`` (execute): mueve huerfanos a ``.trash/``. Precondicion:
  ``LedgerValidator.validate_chain()`` debe pasar.

Ledger Monism (Art. I): el GC NO escribe al ledger — solo mueve blobs.
"""

import gzip
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from causadb._ledger_validator import LedgerValidator

_HARVEST_BLOB_REQUIRED_KEYS = frozenset({"path", "content", "size", "mtime"})


@dataclass
class GarbageCollectionReport:
    executed: bool
    total_blobs: int
    orphan_count: int
    orphans: List[Tuple[str, str]] = field(default_factory=list)
    by_class: Dict[str, int] = field(default_factory=dict)
    moved: List[Tuple[str, str]] = field(default_factory=list)


class BlobGC:
    def __init__(self, ledger_path: str, blob_store_path: str = None):
        if not os.path.isabs(ledger_path):
            raise ValueError(f"ledger_path must be absolute, got: {ledger_path}")
        self.ledger_path = ledger_path
        if blob_store_path is None:
            blob_store_path = os.path.join(os.path.dirname(ledger_path), "blobs")
        self.blob_store_path = blob_store_path

    def collect(self, dry_run: bool = True) -> GarbageCollectionReport:
        if not dry_run:
            result = LedgerValidator(self.ledger_path).validate_chain()
            if not result.is_valid:
                raise RuntimeError(
                    f"Cannot execute GC: ledger validation failed "
                    f"({result.failure_type} at position {result.position}"
                    f"{': ' + result.description if result.description else ''}). "
                    f"Dry-run is available without validation."
                )

        referenced_payload, referenced_snapshot = self._collect_referenced_hashes()
        disk_blobs = self._scan_disk_blobs()
        harvest_hashes = self._identify_harvest_blobs(disk_blobs)

        orphans = []
        by_class = {"payload": 0, "snapshot": 0, "harvest": 0, "orphan": 0}
        for shard, bhash in disk_blobs:
            if bhash in referenced_payload:
                by_class["payload"] += 1
            elif bhash in referenced_snapshot:
                by_class["snapshot"] += 1
            elif bhash in harvest_hashes:
                by_class["harvest"] += 1
            else:
                by_class["orphan"] += 1
                orphans.append((shard, bhash))

        moved = []
        if not dry_run and orphans:
            moved = self._move_orphans_to_trash(orphans)

        return GarbageCollectionReport(
            executed=not dry_run,
            total_blobs=len(disk_blobs),
            orphan_count=len(orphans),
            orphans=orphans,
            by_class=by_class,
            moved=moved,
        )

    def _ledger_files(self) -> List[str]:
        files = []
        archive_dir = os.path.join(os.path.dirname(self.ledger_path), "archive")
        if os.path.isdir(archive_dir):
            archives = sorted(f for f in os.listdir(archive_dir) if f.endswith(".gz"))
            for a in archives:
                files.append(os.path.join(archive_dir, a))
        if os.path.isfile(self.ledger_path):
            files.append(self.ledger_path)
        return files

    def _collect_referenced_hashes(self) -> Tuple[Set[str], Set[str]]:
        payload_refs = set()
        snapshot_refs = set()

        for ledger_file in self._ledger_files():
            is_gz = ledger_file.endswith(".gz")
            try:
                opener = gzip.open if is_gz else open
                with opener(ledger_file, "rt") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event = entry.get("event", {})
                        
                        payload = event.get("payload", {})
                        if isinstance(payload, dict) and "$blob" in payload:
                            blob_hash = payload["$blob"]
                            if isinstance(blob_hash, str):
                                payload_refs.add(blob_hash)
                        pre = event.get("pre_snapshot")
                        if isinstance(pre, str):
                            snapshot_refs.add(pre)
                        post = event.get("post_snapshot")
                        if isinstance(post, str):
                            snapshot_refs.add(post)
            except (OSError, gzip.BadGzipFile):
                continue
        
        # Opción A: Resolver referencias transitivas dentro de los snapshots
        from causadb._blob_store import BlobStore
        store = BlobStore(self.blob_store_path)
        
        all_snapshot_refs = set(snapshot_refs)
        for snap_hash in snapshot_refs:
            try:
                manifest = store.get(snap_hash)
                if isinstance(manifest, dict) and "blob_refs" in manifest:
                    for bhash in manifest["blob_refs"].values():
                        all_snapshot_refs.add(bhash)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue

        return payload_refs, all_snapshot_refs

    def _scan_disk_blobs(self) -> List[Tuple[str, str]]:
        blobs = []
        if not os.path.isdir(self.blob_store_path):
            return blobs
        for shard1 in os.listdir(self.blob_store_path):
            if shard1 == ".trash":
                continue
            s1dir = os.path.join(self.blob_store_path, shard1)
            if not os.path.isdir(s1dir):
                continue
            for shard2 in os.listdir(s1dir):
                s2dir = os.path.join(s1dir, shard2)
                if not os.path.isdir(s2dir):
                    continue
                for fn in os.listdir(s2dir):
                    if fn.endswith(".json"):
                        bhash = fn[:-5]
                        blobs.append((f"{shard1}/{shard2}", bhash))
        return blobs

    def _identify_harvest_blobs(self, disk_blobs):
        harvest_hashes = set()
        for shard, bhash in disk_blobs:
            blob_path = os.path.join(self.blob_store_path, shard, f"{bhash}.json")
            try:
                with open(blob_path) as f:
                    d = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(d, dict) and _HARVEST_BLOB_REQUIRED_KEYS.issubset(d.keys()):
                harvest_hashes.add(bhash)
        return harvest_hashes

    def _move_orphans_to_trash(self, orphans):
        trash_base = os.path.join(self.blob_store_path, ".trash")
        moved = []
        for shard, bhash in orphans:
            src = os.path.join(self.blob_store_path, shard, f"{bhash}.json")
            dst = os.path.join(trash_base, shard, f"{bhash}.json")
            if not os.path.exists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os.rename(src, dst)
                moved.append((shard, bhash))
            except OSError:
                continue
        return moved