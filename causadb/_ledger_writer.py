import hashlib
import json
import os
import threading
import logging
from typing import Optional
from causadb._event_schema import CanonicalEvent
from causadb._config import CausaDBConfig
from causadb._file_lock import lock_ex, unlock


def _snapshot_worker(queue, workspace_dir: str, prev_snapshot=None):
    """Worker de snapshot ejecutado en un subproceso (context ``spawn``).

    Debe ser importable a nivel de módulo: ``spawn`` re-importa el módulo
    en el hijo y picklea la referencia — una closure interna (``def _worker``
    dentro del método) NO es pickeable y rompería ``Process(target=...)``.
    ``fork`` no tiene ese requisito pero DEADLOCKEA dentro del event loop
    de FastMCP (threads/anyio activas heredan locks) — de ahí ``spawn``.
    """
    try:
        from causadb._snapshot import WorkspaceSnapshot
        snap = WorkspaceSnapshot.take(workspace_dir, prev_snapshot)
        queue.put(snap)
    except Exception:
        queue.put(None)

class LedgerWriter:
    def __init__(self, ledger_path: str, config: Optional[CausaDBConfig] = None, on_append=None):
        if not os.path.isabs(ledger_path):
            raise ValueError(f"ledger_path must be absolute, got: {ledger_path}")
        
        self.ledger_path = ledger_path
        self.config = config or CausaDBConfig(ledger_path=ledger_path)
        self.on_append = on_append
        self._lock = threading.Lock()
        self._file_lock_path = ledger_path + ".lock"
        self._last_hash_path = ledger_path + ".last_hash.json"
        if not os.path.exists(self._file_lock_path):
            open(self._file_lock_path, "a+b").close()
        self.last_hash = self._get_last_hash()
        # Auto-snapshot safeguard: once a snapshot times out (or the worker
        # returns None), auto-snapshotting is disabled permanently so
        # append() never blocks under the lock waiting on every event.
        self._snapshot_disabled = False
    
    @classmethod
    def with_ocb_feed(cls, ledger_path, config=None):
        ocb = None
        ocb_failed = False

        def _cb(event, entry):
            nonlocal ocb, ocb_failed
            if ocb_failed:
                return
            try:
                if ocb is None:
                    from causadb._ocb_manager import OCB
                    ocb = OCB.for_ledger(ledger_path, actor_id="ledger")
                ocb.append(event)
            except Exception:
                import logging
                logging.warning("with_ocb_feed callback failed; disabling", exc_info=True)
                ocb_failed = True

        return cls(ledger_path, config=config, on_append=_cb)
            
    def _get_last_hash(self) -> str:
        # 1. Intentar leer de last_hash.json si ledger está vacío
        if not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0:
            if os.path.exists(self._last_hash_path):
                with open(self._last_hash_path, "r") as f:
                    try:
                        return json.load(f)["last_hash"]
                    except (json.JSONDecodeError, KeyError):
                        return "GENESIS"
            return "GENESIS"
        
        # 2. Leer del final del ledger
        with open(self.ledger_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            
            # Seek backwards to find the last valid newline
            pos = size - 1
            # Saltar posible newline al final del archivo
            if pos >= 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    pos -= 1
            
            while pos > 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    break
                pos -= 1
            
            line_start = pos + 1 if pos > 0 else 0
            f.seek(line_start)
            
            line = f.readline().decode().strip()
            if line:
                try:
                    return json.loads(line)["hash"]
                except json.JSONDecodeError:
                    return "GENESIS"
        return "GENESIS"
    
    def _get_next_sequence_number(self) -> int:
        """Leer el último sequence_number del ledger y devolver el siguiente."""
        if not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0:
            return 0
        with open(self.ledger_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            pos = size - 1
            if pos >= 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    pos -= 1
            while pos > 0:
                f.seek(pos)
                if f.read(1) == b'\n':
                    break
                pos -= 1
            line_start = pos + 1 if pos > 0 else 0
            f.seek(line_start)
            line = f.readline().decode().strip()
            if line:
                try:
                    entry = json.loads(line)
                    return entry.get("event", {}).get("sequence_number", -1) + 1
                except (json.JSONDecodeError, KeyError):
                    return 0
        return 0

    def _compute_hash(self, event_data: str, prev_hash: str) -> str:
        return hashlib.sha256((event_data + prev_hash).encode()).hexdigest()

    _SNAPSHOT_TIMEOUT = 5

    def _snapshot_with_timeout(
        self, workspace_dir: str, prev_snapshot: Optional[dict] = None
    ) -> tuple[Optional[dict], Optional[str]]:
        """Take a workspace snapshot with a timeout.

        Returns ``(snapshot, hash)`` or ``(None, None)`` on timeout or
        any failure. Once a timeout occurs (or the worker returns None),
        snapshots are permanently disabled for this writer instance so
        ``append()`` never blocks under the lock.

        The snapshot runs in a ``multiprocessing.Process`` so it can be
        ``terminate()``'d on timeout — same safeguard as the Vigilante
        (``_vigilante._snapshot_with_timeout``).

        The context is ``spawn`` (NOT ``fork``): ``fork`` deadlocks inside
        the FastMCP event loop (threads/anyio active when a ``CallToolRequest``
        is being handled inherit locks the worker then never releases). See
        ``_snapshot_worker``.
        """
        if self._snapshot_disabled:
            return None, None

        import multiprocessing as mp
        from causadb._snapshot import WorkspaceSnapshot
        from causadb._blob_store import BlobStore

        store = BlobStore(self.config.blob_store_path)
        ctx = mp.get_context("spawn")
        queue: "mp.Queue" = ctx.Queue()

        proc = ctx.Process(
            target=_snapshot_worker,
            args=(queue, workspace_dir, prev_snapshot),
            daemon=True,
        )
        proc.start()
        proc.join(timeout=self._SNAPSHOT_TIMEOUT)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            self._snapshot_disabled = True
            return None, None

        try:
            snap = queue.get_nowait()
        except Exception:
            snap = None

        if snap is None:
            self._snapshot_disabled = True
            return None, None

        try:
            snap_hash = WorkspaceSnapshot.store(snap, store, root_dir=workspace_dir)
        except Exception:
            snap_hash = None
        return snap, snap_hash

    def _maybe_auto_snapshot(self, event: CanonicalEvent, payload_dict: dict):
        if self._snapshot_disabled:
            return
        workspace_dir = getattr(self.config, "workspace_dir", None)
        if workspace_dir is None:
            return

        pre_hash = payload_dict.get("pre_snapshot") or event.pre_snapshot
        pre_snap = None
        if pre_hash is None and not self._snapshot_disabled:
            pre_snap, pre_hash = self._snapshot_with_timeout(workspace_dir)
            if pre_hash is not None:
                payload_dict["pre_snapshot"] = pre_hash
        elif pre_hash is not None:
            payload_dict.setdefault("pre_snapshot", pre_hash)

        post_hash = payload_dict.get("post_snapshot") or event.post_snapshot
        if post_hash is None and not self._snapshot_disabled:
            post_snap, post_hash = self._snapshot_with_timeout(
                workspace_dir, prev_snapshot=pre_snap,
            )
            if post_hash is not None:
                payload_dict["post_snapshot"] = post_hash
        elif post_hash is not None:
            payload_dict.setdefault("post_snapshot", post_hash)

        # Reflect onto the frozen event via object.__setattr__.
        object.__setattr__(event, "pre_snapshot", pre_hash)
        object.__setattr__(event, "post_snapshot", post_hash)

    def append(self, event: CanonicalEvent):
        entry = None
        with self._lock:
            with open(self._file_lock_path, "a+b") as lock_file:
                lock_ex(lock_file.fileno())
                try:
                    last_hash = self._get_last_hash()
                    
                    try:
                        from causadb._attribution import validate_source
                        if not validate_source(event.source, event.source_type):
                            raise ValueError(f"Invalid source namespace: {event.source}")
                    except ImportError:
                        pass
                    
                    payload_dict = dict(event.payload)
                    try:
                        from causadb._redactor import redact_payload
                        if self.config.redaction_enabled:
                            payload_dict = redact_payload(payload_dict, self.config)
                    except ImportError:
                        pass

                    if "writes" in payload_dict:
                        self._maybe_auto_snapshot(event, payload_dict)

                    if self.config.blob_store_enabled:
                        payload_bytes = json.dumps(payload_dict, sort_keys=True).encode()
                        if len(payload_bytes) > self.config.blob_store_threshold:
                            from causadb._blob_store import BlobStore
                            store = BlobStore(self.config.blob_store_path)
                            content_hash = store.put(payload_dict)
                            payload_dict = {"$blob": content_hash}
                    
                    event_dict = event.to_dict()
                    event_dict["sequence_number"] = self._get_next_sequence_number()
                    event_dict["payload"] = payload_dict
                    event_json = json.dumps(event_dict, sort_keys=True)
                    new_hash = self._compute_hash(event_json, last_hash)
                    entry = {
                        "event": event_dict,
                        "prev_hash": last_hash,
                        "hash": new_hash,
                    }
                    
                    with open(self.ledger_path, "a") as f:
                        f.write(json.dumps(entry, sort_keys=True) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                    
                    # Actualizar last_hash.json
                    with open(self._last_hash_path, "w") as f:
                        json.dump({"last_hash": new_hash}, f)
                        f.flush()
                        os.fsync(f.fileno())
                    
                    self.last_hash = new_hash
                finally:
                    unlock(lock_file.fileno())
        
        # Lock released here, post-flush.
        if entry is not None and self.on_append is not None:
             try:
                 self.on_append(event, entry)
             except Exception:
                 import logging
                 logging.warning("LedgerWriter on_append callback failed", exc_info=True)
                 
        return entry
