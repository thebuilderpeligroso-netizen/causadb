"""Modo Vigilante — filesystem watcher that logs file changes to the ledger.

F.2.5 — Uses `watchfiles` for cross-platform filesystem events and `pathspec`
for `.gitignore`-style exclusion patterns.

F.12.1 — Takes pre/post workspace snapshots on every logged event and
records their blob hashes on the `CanonicalEvent`.

Exposed API
-----------
- `VigilanteWatcher`: thread-based watcher that logs every filesystem change
  as a `CanonicalEvent` with `event_type=FILE_MODIFIED` to the ledger.
"""

import os
import threading
from typing import List, Optional, Set

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter

try:
    import pathspec
except ImportError:  # pragma: no cover
    pathspec = None  # type: ignore[assignment]

try:
    from watchfiles import watch
except ImportError:  # pragma: no cover
    watch = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Default excludes — directories / prefixes always skipped
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDED_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".egg-info",
    ".pytest_cache",
    "__pycache__",
}

_DEFAULT_EXCLUDED_PREFIXES: Set[str] = {
    ".",  # any hidden file/directory by default (besides .gitignore itself)
}

# watchfiles change-type constants
_CHANGE_ADDED = 1
_CHANGE_MODIFIED = 2
_CHANGE_DELETED = 3

_ACTION_MAP = {
    _CHANGE_ADDED: "created",
    _CHANGE_MODIFIED: "modified",
    _CHANGE_DELETED: "deleted",
}


class VigilanteWatcher:
    """Observes a directory and logs every filesystem change to the CausaDB ledger.

    Parameters
    ----------
    ledger_path : str
        Absolute path to the ledger log file.
    watch_dir : str
        Absolute path to the directory to watch.
    stop_event : threading.Event, optional
        External stop signal. If not provided, an internal one is created.
    extra_excludes : list of str, optional
        Additional gitignore-style patterns to exclude.
    """

    def __init__(
        self,
        ledger_path: str,
        watch_dir: str,
        stop_event: Optional[threading.Event] = None,
        extra_excludes: Optional[List[str]] = None,
        skip_baseline: bool = False,
    ):
        if not os.path.isabs(ledger_path):
            raise ValueError(f"ledger_path must be absolute, got: {ledger_path}")
        if not os.path.isabs(watch_dir):
            raise ValueError(f"watch_dir must be absolute, got: {watch_dir}")

        self.ledger_path = ledger_path
        self.watch_dir = watch_dir
        self.stop_event = stop_event or threading.Event()
        self.extra_excludes = extra_excludes or []

        self._writer = LedgerWriter(ledger_path)
        self._gitignore_spec = self._load_gitignore(watch_dir)
        self._known_files: Set[str] = set()

        # F.12.1 — BlobStore for workspace snapshots. Reuse the writer's
        # configured blob_store_path when available; otherwise default to a
        # ``blobs`` dir next to the ledger.
        self._blob_store = self._init_blob_store()
        # Snapshot flag: starts False, set to True after first timeout
        # (prevents O(n) walk on every event for large workspaces).
        self._snapshot_disabled = False
        # Baseline snapshot taken at construction; used as the "pre" for the
        # first event. Updated to the latest post-snapshot after each event.
        if skip_baseline:
            # Bypass the blocking fork subprocess in __init__. Snapshot
            # behaviour is covered by its own tests; the CLI start/stop
            # lifecycle test only exercises thread management.  (deuda #14)
            self._last_snapshot = None
            self._snapshot_disabled = True
        else:
            self._last_snapshot = self._take_baseline()
            # If the baseline timed out, disable snapshots entirely to avoid
            # waiting on every event (Artículo V).
            if self._last_snapshot is None:
                self._snapshot_disabled = True

    def _init_blob_store(self):
        """Build a BlobStore for snapshot persistence."""
        from causadb._blob_store import BlobStore
        config = getattr(self._writer, "config", None)
        if config is not None and getattr(config, "blob_store_enabled", False):
            return BlobStore(config.blob_store_path)
        if config is not None:
            return BlobStore(config.blob_store_path)
        return BlobStore(os.path.join(os.path.dirname(self.ledger_path), "blobs"))

    _SNAPSHOT_TIMEOUT = 5

    def _snapshot_with_timeout(
        self, prev_snapshot: Optional[dict] = None
    ) -> tuple[Optional[dict], Optional[str]]:
        """Take a workspace snapshot with a timeout.

        Returns ``(snapshot, hash)`` or ``(None, None)`` on timeout or
        any failure. Once a timeout occurs, snapshots are permanently
        disabled for this watcher instance to avoid waiting on every
        single event.

        The snapshot runs in a **multiprocessing.Process** so it can be
        ``terminate()``'d on timeout — no zombie threads. This is safe
        from both the main thread and watcher sub-threads because
        multiprocessing does not share the same signal restrictions as
        ``threading + signal.alarm``.
        """
        if self._snapshot_disabled:
            return None, None

        import multiprocessing as mp
        from causadb._snapshot import WorkspaceSnapshot

        ctx = mp.get_context("fork")
        queue: "mp.Queue" = ctx.Queue()

        def _worker(q):
            try:
                snap = WorkspaceSnapshot.take(self.watch_dir, prev_snapshot)
                q.put(snap)
            except Exception:
                q.put(None)

        proc = ctx.Process(target=_worker, args=(queue,), daemon=True)
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
            snap_hash = WorkspaceSnapshot.store(
                snap, self._blob_store, root_dir=self.watch_dir
            )
        except Exception:
            snap_hash = None
        return snap, snap_hash

    def _take_baseline(self):
        """Take an initial snapshot with a timeout.

        Returns the snapshot dict (or ``None`` on timeout/failure). Used as
        the ``pre`` for the first logged event. Sin baseline el primer evento
        tendrá ``pre_snapshot=None``, que es aceptable — Artículo V.
        """
        snap, _ = self._snapshot_with_timeout()
        return snap

    # ------------------------------------------------------------------
    # Gitignore loading
    # ------------------------------------------------------------------

    def _load_gitignore(self, watch_dir: str):
        """Load ``.gitignore`` from *watch_dir* via *pathspec*.

        Returns ``None`` when *pathspec* is unavailable or no ``.gitignore``
        file exists.
        """
        if pathspec is None:
            return None

        gitignore_path = os.path.join(watch_dir, ".gitignore")
        if not os.path.isfile(gitignore_path):
            return None

        try:
            with open(gitignore_path, "r") as f:
                lines = f.readlines()
            return pathspec.GitIgnoreSpec.from_lines(lines)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Exclusion logic
    # ------------------------------------------------------------------

    def _is_excluded(self, abs_path: str) -> bool:
        """Return ``True`` if the path should **not** be logged.

        Checks in order:
        1. Default excluded directories (e.g. ``.git``, ``__pycache__``).
        2. Hidden-file prefix (leading dot) — with an explicit carve-out for
           ``.gitignore`` itself.
        3. ``.gitignore`` patterns via *pathspec*.
        4. Any patterns passed via *extra_excludes* (treated as gitignore-style).
        """
        # Normalise to forward-slash relative path for gitignore matching
        rel = os.path.relpath(abs_path, self.watch_dir)

        # Carve-out: .gitignore itself is never excluded
        if rel == ".gitignore":
            return False

        # 1. Check if path is inside any default-excluded directory
        parts = rel.replace("\\", "/").split("/")
        for part in parts:
            if part in _DEFAULT_EXCLUDED_DIRS:
                return True

        # 2. Check hidden prefix (leading dot on any path component)
        for part in parts:
            if part.startswith("."):
                return True

        # 3. Check .gitignore patterns
        if self._gitignore_spec is not None:
            if self._gitignore_spec.match_file(rel):
                return True

        # 4. Check extra excludes
        if self.extra_excludes:
            try:
                extra_spec = pathspec.GitIgnoreSpec.from_lines(self.extra_excludes)
                if extra_spec.match_file(rel):
                    return True
            except Exception:
                pass

        return False

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def _log_event(self, action: str, path: str):
        """Build a ``CanonicalEvent`` and append it to the ledger.

        F.12.1 — Takes pre/post workspace snapshots and records their blob
        hashes on the event. ``pre`` is the last known snapshot (baseline
        at startup, or the post-snapshot of the previous event). ``post``
        is the current state, taken with ``prev=pre`` for the incremental
        fast-path (O(k) where k = files actually changed).

        Degradación suave (Artículo V): si el snapshot excede el timeout
        configurado (``_SNAPSHOT_TIMEOUT``), el evento se registra igual
        con ``pre_snapshot=None`` y ``post_snapshot=None``.
        """
        from causadb._snapshot import WorkspaceSnapshot

        pre_snapshot = self._last_snapshot
        pre_snapshot_hash = None
        if pre_snapshot is not None:
            try:
                pre_snapshot_hash = WorkspaceSnapshot.store(
                    pre_snapshot, self._blob_store,
                )
            except Exception:
                pre_snapshot_hash = None

        post_snapshot, post_snapshot_hash = self._snapshot_with_timeout(
            prev_snapshot=pre_snapshot,
        )

        payload = {
            "action": action,
            "path": path,
        }
        event = CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="vigilante",
            source="causadb:vigilante",
            source_type="agent",
            payload=payload,  # type: ignore[arg-type]
            pre_snapshot=pre_snapshot_hash,
            post_snapshot=post_snapshot_hash,
        )
        self._writer.append(event)

        if post_snapshot is not None:
            self._last_snapshot = post_snapshot

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self):
        """Enter the watch loop — blocks until ``stop_event`` is set.

        This method is designed to run in a dedicated thread.

        ``debounce=200`` keeps latency low while still grouping rapid bursts
        of changes. The default ``debounce=1600`` would delay file logging by
        over a second.

        ``recursive=False`` avoids overwhelming inotify when the workspace
        contains thousands of subdirectories (``Master/`` has ~4300 dirs).
        The shallow watch is sufficient because the vigilante also takes
        periodic full-workspace snapshots (F.12.1), which capture deep
        changes even if inotify misses them at the directory level.
        """
        if watch is None:
            raise RuntimeError("watchfiles is not installed — cannot start Vigilante")

        for changes in watch(
            self.watch_dir,
            stop_event=self.stop_event,
            raise_interrupt=False,
            recursive=False,
            debounce=200,
        ):
            for change_type, path in changes:
                if self._is_excluded(path):
                    continue

                action = _ACTION_MAP.get(change_type, "modified")
                self._log_event(action, path)

    def stop(self):
        """Signal the watcher to stop."""
        self.stop_event.set()
