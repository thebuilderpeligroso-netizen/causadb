import glob
import hashlib
import json
import os
import threading
import time
import logging
from typing import Optional
from causadb._event_schema import CanonicalEvent
from causadb._config import CausaDBConfig
from causadb._file_lock import lock_ex, unlock


class CorruptionError(Exception):
    """Cadena rota-parseable: fail-closed, jamás GENESIS/seq-0."""
    pass


# Fase 4 B-01: lectura del tail por bloques (8-64KB en memoria),
# jamás byte-a-byte.
_TAIL_BLOCK_SIZE = 8 * 1024

# Fase 4 B-01: cap de backups .corrupt-* (conservar últimos 5).
_CORRUPT_KEEP = 5


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
            
    def _quarantine_tail(self, tail_len: int) -> None:
        """Rescata los últimos ``tail_len`` bytes a ``.corrupt-<ts>`` y los
        trunca del ledger. Cap: conserva los últimos 5 (``_CORRUPT_KEEP``).

        NOTA: append al ledger sigue siendo lock+write+fsync directo —
        SIN temp+rename (el rename rompería el offset de lectores con el
        fd abierto). Solo el sidecar ``.last_hash.json`` usa tmp+fsync+replace.
        """
        size = os.path.getsize(self.ledger_path)
        with open(self.ledger_path, "rb") as f:
            f.seek(size - tail_len)
            bad = f.read(tail_len)
        corrupt_path = f"{self.ledger_path}.corrupt-{time.time_ns()}"
        with open(corrupt_path, "wb") as f:
            f.write(bad)
            f.flush()
            os.fsync(f.fileno())
        with open(self.ledger_path, "r+b") as f:
            f.truncate(size - tail_len)
            f.flush()
            os.fsync(f.fileno())
        existing = sorted(glob.glob(self.ledger_path + ".corrupt-*"))
        for old in existing[:-_CORRUPT_KEEP]:
            try:
                os.unlink(old)
            except OSError:
                pass

    def _read_tail(self) -> Optional[dict]:
        """Helper único para hash Y sequence: lee la última entrada válida.

        - Sin ``\\n`` final, JSON inválido, o sin hash/seq (con ``event``
          presente) → cuarentena (backup ``.corrupt-<ts>`` + truncate)
          y reintenta (el próximo append queda válido y encadena).
        - Si parsea pero rompe la cadena (hash auto-inconsistente o
          ``prev_hash`` discontinuo) → ``raise CorruptionError``
          (fail-closed, jamás GENESIS/seq-0).
        - Lectura por bloques (``_TAIL_BLOCK_SIZE``), no byte-a-byte.
        - Líneas legacy sin clave ``event`` (sintéticas, sin formato
          real): se toleran sin verificar ni cuarentenar (compat con
          benchmarks históricos); solo exponen ``hash``.
        """
        for _ in range(10):
            if not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0:
                return None
            with open(self.ledger_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(size - 1)
                has_trailing_nl = f.read(1) == b"\n"
                # Juntar bloques hacia atrás hasta tener 2 líneas
                # completas (tail + previa para continuidad) o BOF.
                data = b""
                pos = size
                need = 2 if has_trailing_nl else 1
                while pos > 0 and data.count(b"\n") < need:
                    step = min(_TAIL_BLOCK_SIZE, pos)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
                bof = (pos == 0)
            parts = data.split(b"\n")
            if has_trailing_nl:
                last_raw = parts[-2] if len(parts) >= 2 else b""
                prev_raw = parts[-3] if len(parts) >= 3 else None
                tail_len = len(last_raw) + 1
                prev_complete = bof or prev_raw is not None
            else:
                last_raw = parts[-1]
                prev_raw = parts[-2] if len(parts) >= 2 else None
                tail_len = len(last_raw)
                prev_complete = bof or (prev_raw is not None and len(parts) >= 2 and (bof or data.count(b"\n") >= 1))
                # prev_raw es completa si BOF alcanzado o hay \n previo;
                # con need=1 puede estar cortada: solo usarla si bof.
                if not bof:
                    prev_raw = None
                    prev_complete = False
            if not has_trailing_nl:
                # Media línea (corte de luz) → cuarentena.
                self._quarantine_tail(tail_len)
                continue
            try:
                entry = json.loads(last_raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._quarantine_tail(tail_len)
                continue
            if not isinstance(entry, dict) or "hash" not in entry:
                self._quarantine_tail(tail_len)
                continue
            ev = entry.get("event")
            if not isinstance(ev, dict):
                # Legacy sintético (sin formato real): tolerar.
                return entry
            if "sequence_number" not in ev:
                self._quarantine_tail(tail_len)
                continue
            # Auto-consistencia del hash.
            event_json = json.dumps(ev, sort_keys=True)
            expected = self._compute_hash(event_json, entry.get("prev_hash", ""))
            if entry.get("hash") != expected:
                raise CorruptionError(
                    f"hash mismatch en tail (esperado {expected[:12]}…)"
                )
            # Continuidad contra la previa (solo si la tenemos completa
            # y el ledger trae ≥2 líneas en el archivo actual).
            if prev_raw is not None and prev_complete and prev_raw.strip():
                try:
                    prev_entry = json.loads(prev_raw.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise CorruptionError("línea previa inválida (cadena rota)")
                if not isinstance(prev_entry, dict) or "hash" not in prev_entry:
                    raise CorruptionError("línea previa sin hash (cadena rota)")
                if entry.get("prev_hash") != prev_entry.get("hash"):
                    raise CorruptionError(
                        f"continuity break: prev_hash {str(entry.get('prev_hash'))[:12]}… "
                        f"!= {str(prev_entry.get('hash'))[:12]}…"
                    )
            return entry
        raise CorruptionError("tail ilegible tras cuarentenas reiteradas")

    def _write_last_hash_sidecar(self, new_hash: str) -> None:
        """Sidecar SÍ atómico: tmp+fsync+os.replace."""
        tmp = f"{self._last_hash_path}.tmp-{os.getpid()}-{time.time_ns()}"
        with open(tmp, "w") as f:
            json.dump({"last_hash": new_hash}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._last_hash_path)

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

        # 2. Tail único (cuarentena truncation, CorruptionError si roto).
        entry = self._read_tail()
        if entry is None:
            if os.path.exists(self._last_hash_path):
                with open(self._last_hash_path, "r") as f:
                    try:
                        return json.load(f)["last_hash"]
                    except (json.JSONDecodeError, KeyError):
                        return "GENESIS"
            return "GENESIS"
        try:
            return entry["hash"]
        except KeyError:
            return "GENESIS"
    
    def _get_next_sequence_number(self) -> int:
        """Leer el último sequence_number del ledger y devolver el siguiente."""
        if not os.path.exists(self.ledger_path) or os.path.getsize(self.ledger_path) == 0:
            return 0
        entry = self._read_tail()
        if entry is None:
            return 0
        try:
            ev = entry.get("event", {})
            if not isinstance(ev, dict) or "sequence_number" not in ev:
                return 0  # legacy sintético sin formato real
            return ev.get("sequence_number", -1) + 1
        except (AttributeError, KeyError, TypeError):
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
        """Anexa un evento en dos fases (orden auditable, no invertible).

        Fase 1 — FUERA del lock: validacion de source, redaccion,
        auto-snapshots (pre/post, timeout ``_SNAPSHOT_TIMEOUT`` c/u) y
        externalizacion al BlobStore. Es lo lento (hasta ~10s) y por eso
        NO puede correr bajo ``lock_ex``: bloquearia a los demas
        appends concurrentes.

        Fase 2 — BAJO ``lock_ex``: SOLO ``_get_last_hash`` +
        ``_get_next_sequence_number`` + write+fsync + sidecar
        (los 4 ya atomicos / ya existentes, sin cambios).

        NO invertir: el snapshot debe ser ANTERIOR a su propia linea
        (causalidad pre/post preservada); un snapshot posterior
        romperia la semantica pre/post.

        Skew posible documentado: el snapshot refleja el workspace en
        T_pre (pre-lock), no en el instante exacto del write bajo lock.
        Con escritores concurrentes, el orden de secuencia del ledger
        puede no coincidir con el orden wall-clock de los snapshots.
        Aceptado por diseño: cada linea siempre lleva un snapshot
        anterior a ella misma, nunca posterior.
        """
        # ---- Fase 1: fuera de todo lock (lento, paralelizable) ----
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
                content_hash = store.put_redacted(payload_dict, self.config)
                payload_dict = {"$blob": content_hash}

        event_dict = event.to_dict()
        event_dict["payload"] = payload_dict

        # ---- Fase 2: bajo lock_ex, solo tail + write + sidecar ----
        entry = None
        with self._lock:
            with open(self._file_lock_path, "a+b") as lock_file:
                lock_ex(lock_file.fileno())
                try:
                    last_hash = self._get_last_hash()

                    event_dict["sequence_number"] = self._get_next_sequence_number()
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

                    # Actualizar last_hash.json (SÍ atómico: tmp+fsync+replace)
                    self._write_last_hash_sidecar(new_hash)

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
