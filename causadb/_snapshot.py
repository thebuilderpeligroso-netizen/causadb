"""F.12.1 — Workspace snapshots.

A ``WorkspaceSnapshot`` is a content-addressed tree of the files in a
workspace directory. Each file is hashed with ``blake2b`` (stdlib) and
its content is stored in the existing ``BlobStore`` keyed by that hash,
so identical files dedup automatically. The snapshot manifest itself is
also stored as a blob, so a snapshot is fully reconstructible from its
manifest hash.

Incremental fast-path: when a previous snapshot is supplied to ``take``,
files whose ``st_mtime`` AND ``st_size`` are unchanged reuse the previous
hash without re-reading the file. Only new or modified files are re-hashed,
reducing the cost from O(n) to O(k) where k = files actually changed.

Anti-secrets: ``.env`` and ``.env.*`` are ALWAYS excluded, even if the
caller tries to include them via ``DEFAULT_EXCLUDES`` overrides.

Gitignore: when a ``.gitignore`` exists in the root directory, its
patterns are honored via ``pathspec`` (same semantics as the Vigilante).
If ``pathspec`` is not installed the ``.gitignore`` is silently ignored.
"""

import base64
import hashlib
import os
from datetime import datetime
from typing import Optional

from causadb._blob_store import BlobStore

try:
    import pathspec
except ImportError:  # pragma: no cover
    pathspec = None  # type: ignore[assignment]


# Tamaño máximo de archivo individual que se hashea y persiste como content
# blob. Archivos por encima de este límite se registran en el manifest con
# hash=None y NO se crea content blob (anti-OOM, paridad con
# _harvest_source_filesystem.MAX_BLOB_SIZE).
SNAPSHOT_BYTE_CAP = 10 * 1024 * 1024  # 10 MB

# Cantidad de bytes iniciales que se leen para detectar si un archivo es
# binario (presencia de bytes nulos). Si los primeros bytes contienen un
# byte nulo, se trata como binario: hash=None, sin content blob.
_SNAPSHOT_BINARY_SNIFF_BYTES = 2048


class WorkspaceSnapshot:
    """Content-addressed workspace tree snapshot with dedup + incremental hashing."""

    DEFAULT_EXCLUDES = frozenset({
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".egg-info",
        ".pytest_cache",
        # CausaDB internal artifacts — a workspace whose ledger lives inside
        # it must never snapshot its own state (ledger, ocb partitions,
        # blob store).
        ".causadb",
        "ocb",
        "blobs",
    })

    # Always excluded for anti-secrets — never appears in a snapshot.
    ALWAYS_EXCLUDES = frozenset({".env"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_env_file(name: str) -> bool:
        """Return True for ``.env`` and any ``.env.*`` filename."""
        if name == ".env":
            return True
        return name.startswith(".env.")

    @classmethod
    def _is_excluded_component(cls, component: str) -> bool:
        """True if a single path component (file or dir name) is excluded."""
        if cls._is_env_file(component):
            return True
        # .egg-info is a suffix-style dir: foo.egg-info
        if component.endswith(".egg-info"):
            return True
        return component in cls.DEFAULT_EXCLUDES

    @classmethod
    def _should_exclude_rel(cls, rel_path: str) -> bool:
        """True if any component of the relative path is excluded."""
        parts = rel_path.replace("\\", "/").split("/")
        for part in parts:
            if cls._is_excluded_component(part):
                return True
        return False

    @classmethod
    def _load_gitignore(cls, root_dir: str):
        """Load ``.gitignore`` from *root_dir* via *pathspec*.

        Returns ``None`` when *pathspec* is unavailable or no ``.gitignore``
        file exists.
        """
        if pathspec is None:
            return None

        gitignore_path = os.path.join(root_dir, ".gitignore")
        if not os.path.isfile(gitignore_path):
            return None

        try:
            with open(gitignore_path, "r") as f:
                lines = f.readlines()
            return pathspec.GitIgnoreSpec.from_lines(lines)
        except Exception:
            return None

    @staticmethod
    def _hash_file(abs_path: str) -> str:
        h = hashlib.blake2b(digest_size=32)
        with open(abs_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _hash_or_none(abs_path: str) -> Optional[str]:
        """Hash a file with blake2b, or return None if it's binary.

        Sniffs the first ``_SNAPSHOT_BINARY_SNIFF_BYTES`` bytes for a null
        byte; if found, returns None (no content blob). Otherwise hashes
        the full file.
        """
        try:
            with open(abs_path, "rb") as f:
                sniff = f.read(_SNAPSHOT_BINARY_SNIFF_BYTES)
                if b"\x00" in sniff:
                    return None
                h = hashlib.blake2b(digest_size=32)
                h.update(sniff)
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
                return h.hexdigest()
        except (OSError, MemoryError):
            return None

    @staticmethod
    def _read_file(abs_path: str) -> bytes:
        with open(abs_path, "rb") as f:
            return f.read()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def take(cls, root_dir: str, prev_snapshot: Optional[dict] = None) -> dict:
        """Walk *root_dir* and return a snapshot dict.

        Each file entry is ``{"hash": str, "size": int, "mtime": int}``.

        When *prev_snapshot* is supplied, the incremental fast-path is used:
        files whose ``st_mtime`` and ``st_size`` match the previous entry
        reuse its hash without re-reading the file.
        """
        prev_files = prev_snapshot.get("files", {}) if prev_snapshot else {}
        gitignore_spec = cls._load_gitignore(root_dir)

        files = {}
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Prune excluded dirs in-place so os.walk does not descend.
            dirnames[:] = [
                d for d in dirnames if not cls._is_excluded_component(d)
            ]
            for fname in filenames:
                if cls._is_env_file(fname):
                    continue
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, root_dir).replace("\\", "/")
                if cls._should_exclude_rel(rel_path):
                    continue
                if gitignore_spec is not None and gitignore_spec.match_file(rel_path):
                    continue
                try:
                    st = os.stat(abs_path)
                except OSError:
                    continue
                size = st.st_size
                mtime = int(st.st_mtime)

                prev_entry = prev_files.get(rel_path)
                if (prev_entry is not None
                        and prev_entry.get("size") == size
                        and prev_entry.get("mtime") == mtime):
                    file_hash = prev_entry["hash"]
                elif size > SNAPSHOT_BYTE_CAP:
                    file_hash = None
                else:
                    file_hash = cls._hash_or_none(abs_path)

                files[rel_path] = {
                    "hash": file_hash,
                    "size": size,
                    "mtime": mtime,
                }

        return {
            "type": "snapshot",
            "files": files,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

    @classmethod
    def store(cls, snapshot: dict, blob_store: BlobStore,
              root_dir: Optional[str] = None) -> str:
        """Persist *snapshot* into *blob_store*.

        Each file's content is stored as a blob keyed by its blake2b hash
        (so identical content dedups to a single blob). The BlobStore
        returns its own SHA-256 content hash for each blob; we record that
        mapping in ``snapshot["blob_refs"]`` so ``restore`` can fetch the
        content by blake2b hash. The snapshot manifest is then stored and
        its blob hash returned.

        When *root_dir* is supplied, file contents are read from disk and
        stored. When omitted, only the manifest is stored (content blobs
        are assumed to already exist from a prior ``store`` call).
        """
        blob_refs = dict(snapshot.get("blob_refs", {}))
        if root_dir is not None:
            for rel_path, entry in snapshot.get("files", {}).items():
                file_hash = entry["hash"]
                if file_hash is None:
                    continue
                if file_hash in blob_refs:
                    # Already stored (dedup) — skip.
                    continue
                abs_path = os.path.join(root_dir, rel_path)
                try:
                    content = cls._read_file(abs_path)
                except OSError:
                    continue
                blob_sha = blob_store.put({
                    "file_hash": file_hash,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                })
                blob_refs[file_hash] = blob_sha
        snapshot["blob_refs"] = blob_refs
        return blob_store.put(snapshot)

    @classmethod
    def restore(cls, snapshot_hash: str, blob_store: BlobStore,
                target_dir: str) -> dict:
        """Restore the workspace at *target_dir* to the snapshot state.

        Files present in the snapshot are written back from their content
        blobs; files NOT in the snapshot but present in *target_dir* are
        removed. Returns the snapshot dict that was restored.
        """
        snapshot = blob_store.get(snapshot_hash)
        os.makedirs(target_dir, exist_ok=True)

        snap_files = set(snapshot.get("files", {}).keys())

        # Remove files in target_dir that are not in the snapshot.
        for dirpath, dirnames, filenames in os.walk(target_dir, topdown=False):
            dirnames[:] = [
                d for d in dirnames if not cls._is_excluded_component(d)
            ]
            for fname in filenames:
                if cls._is_env_file(fname):
                    continue
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, target_dir).replace("\\", "/")
                if rel_path not in snap_files:
                    try:
                        os.remove(abs_path)
                    except OSError:
                        pass

        # Write back each file from its content blob.
        blob_refs = snapshot.get("blob_refs", {})
        for rel_path, entry in snapshot.get("files", {}).items():
            file_hash = entry["hash"]
            blob_sha = blob_refs.get(file_hash)
            if blob_sha is None:
                continue
            content_blob = blob_store.get(blob_sha)
            content = base64.b64decode(content_blob["content_b64"])
            abs_path = os.path.join(target_dir, rel_path)
            parent = os.path.dirname(abs_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(content)

        return snapshot

    @classmethod
    def diff(cls, pre_hash: str, post_hash: str, blob_store: BlobStore) -> list:
        """Return a list of ``{"path": str, "action": "modified"|"added"|"deleted"}``."""
        pre = blob_store.get(pre_hash)
        post = blob_store.get(post_hash)
        pre_files = pre.get("files", {})
        post_files = post.get("files", {})

        results = []
        all_paths = set(pre_files.keys()) | set(post_files.keys())
        for path in all_paths:
            in_pre = path in pre_files
            in_post = path in post_files
            if in_pre and in_post:
                if pre_files[path]["hash"] != post_files[path]["hash"]:
                    results.append({"path": path, "action": "modified"})
            elif in_post and not in_pre:
                results.append({"path": path, "action": "added"})
            else:
                results.append({"path": path, "action": "deleted"})
        return results
