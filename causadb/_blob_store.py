import hashlib
import json
import os
from typing import Optional


class BlobNotFoundError(FileNotFoundError):
    """$blob referencia un hash nunca persistido (BIT-CHR.35 P2).

    Subclase de ``FileNotFoundError``: backward-compat total con callers
    que atrapan ``FileNotFoundError`` (R4 — política blob faltante permite
    a ``revive`` distinguir blobs faltantes de errores duros y degradar
    con banner en vez de morir).
    """


class BlobStore:
    """Content-addressed blob storage with 2-level sharding.

    Every blob is persisted to disk under
    ``base_path / hash[:2] / hash[2:4] / {hash}.json``.

    The *threshold* parameter is **not** enforced by ``put()`` — it is
    informational and intended for the caller (e.g. ``LedgerWriter``) to
    decide whether to emit a ``$blob`` reference or inline the data.
    """

    def __init__(self, base_path: str, threshold: int = 1024):
        self.base_path = base_path
        self.threshold = threshold

    def put(self, data: dict) -> str:
        """Persist *data* on disk under its SHA-256 content hash.

        Idempotent: if the blob already exists on disk, the existing file is
        left untouched and the content hash is returned without re-writing.
        Atomic: the blob is written to a temp file in the same directory and
        moved into place with ``os.replace``, so a concurrent reader never
        observes a partially written file. The on-disk bytes match the
        content hash (canonical JSON, ``sort_keys=True``).
        Returns the content hash.
        """
        payload_bytes = json.dumps(data, sort_keys=True).encode()
        content_hash = hashlib.sha256(payload_bytes).hexdigest()
        shard = f"{content_hash[:2]}/{content_hash[2:4]}"
        blob_dir = os.path.join(self.base_path, shard)
        blob_path = os.path.join(blob_dir, f"{content_hash}.json")
        if os.path.exists(blob_path):
            return content_hash
        os.makedirs(blob_dir, exist_ok=True)
        temp_path = f"{blob_path}.tmp.{os.getpid()}.{os.urandom(4).hex()}"
        try:
            with open(temp_path, "w") as f:
                json.dump(data, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, blob_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        return content_hash

    def get(self, content_hash: str) -> dict:
        """Retrieve the blob identified by *content_hash* from disk.

        Raises ``FileNotFoundError`` when the blob does not exist.
        """
        shard = f"{content_hash[:2]}/{content_hash[2:4]}"
        blob_path = os.path.join(self.base_path, shard, f"{content_hash}.json")
        with open(blob_path) as f:
            return json.load(f)


def resolve_payload(payload: dict, blob_store: Optional[BlobStore]) -> dict:
    """Resolve a ``$blob`` reference into its materialized payload.

    Behavior:
      - If *payload* is a dict with a ``$blob`` key and *blob_store* is not
        None, the referenced blob is loaded from disk and returned.
      - If the blob does NOT exist on disk, a descriptive
        ``BlobNotFoundError`` (subclase de ``FileNotFoundError``, R4) is
        raised (fail-fast, BIT-CHR.35 P2) including
        the missing blob hash, the derived blob path, and the ledger
        directory (derived from ``blob_store.base_path``). This replaces the
        previous silent-degradation behavior of returning ``{}``.
      - If *blob_store* is None, the ``$blob`` reference is returned as-is
        (caller did not request resolution).
      - Payloads without a ``$blob`` key are returned unchanged.

    The ledger directory is derived as ``dirname(blob_store.base_path)``
    (the blob store lives at ``<ledger_dir>/blobs``), so the signature stays
    unchanged — ``resolve_payload`` does not need the ledger path passed in.
    """
    if isinstance(payload, dict) and "$blob" in payload and blob_store is not None:
        blob_hash = payload["$blob"]
        try:
            return blob_store.get(blob_hash)
        except FileNotFoundError:
            # Re-raise with a descriptive message (BIT-CHR.35 P2): include
            # the missing hash, the derived blob path, and the ledger
            # directory (derived from blob_store.base_path) so the operator
            # can locate the corruption without guessing.
            shard = f"{blob_hash[:2]}/{blob_hash[2:4]}"
            blob_path = os.path.join(blob_store.base_path, shard, f"{blob_hash}.json")
            ledger_dir = os.path.dirname(blob_store.base_path)
            raise BlobNotFoundError(
                f"Blob {blob_hash} referenced by $blob not found on disk. "
                f"Expected blob at: {blob_path}. "
                f"Ledger directory: {ledger_dir}. "
                f"This indicates a corrupted or incomplete workspace — the "
                f"ledger references a blob that was never persisted (or was "
                f"deleted). Run `causadb validate` to check ledger integrity."
            ) from None
    return payload
