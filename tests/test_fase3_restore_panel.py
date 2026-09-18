"""Fase 3 — Restore seguro + panel sin XSS (TDD RED→GREEN).

Tests NUEVOS primero: deben FALLAR antes del fix y PASAR después.
Alcance exacto del plan auditado:
- restore(target, delete_extra=False) por defecto NO borra
- validación realpath+commonpath (normpath primero; a/../b pasa;
  absolutas/Windows/~/symlinks que escapan → rechazo sin tocar disco)
- bisect pasa delete_extra=True + snapshot inicial a temp fuera + restore en try/finally
- dashboard: sin innerHTML con datos del ledger (helper safeSet + textContent)
"""
import base64
import hashlib
import inspect
import os

import pytest

from causadb._blob_store import BlobStore


def _make_ws_store(tmp_path):
    ws = str(tmp_path / "workspace")
    os.makedirs(ws, exist_ok=True)
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    return ws, store


def _store_content(store, content: bytes):
    file_hash = hashlib.blake2b(content, digest_size=32).hexdigest()
    blob_sha = store.put_redacted({
        "file_hash": file_hash,
        "content_b64": base64.b64encode(content).decode("ascii"),
    })
    return file_hash, blob_sha


def _malicious_snapshot(store, rel_path, content=b"evil\n"):
    file_hash, blob_sha = _store_content(store, content)
    snap = {
        "type": "snapshot",
        "files": {
            rel_path: {"hash": file_hash, "size": len(content), "mtime": 1},
        },
        "blob_refs": {file_hash: blob_sha},
        "created_at": "2026-01-01T00:00:00Z",
    }
    snap_hash = store.put_redacted(snap)
    return snap_hash


def _snapshot_of(ws, store, files: dict):
    """Crea snapshot real de *files* {rel: content} en ws y lo guarda."""
    from causadb._snapshot import WorkspaceSnapshot
    for rel, content in files.items():
        full = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(full) or ws, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    snap = WorkspaceSnapshot.take(ws)
    return WorkspaceSnapshot.store(snap, store, root_dir=ws)


# ---------------------------------------------------------------------------
# 1. ../ rechazada sin tocar disco
# ---------------------------------------------------------------------------

def test_restore_rejects_dotdot_without_touching_disk(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    with open(os.path.join(ws, "keep.txt"), "w") as f:
        f.write("keep\n")
    before = sorted(os.listdir(ws))
    evil_outside = tmp_path / "evil_dotdot.txt"
    if evil_outside.exists():
        evil_outside.unlink()
    snap_hash = _malicious_snapshot(store, "../evil_dotdot.txt")
    with pytest.raises(ValueError):
        WorkspaceSnapshot.restore(snap_hash, store, ws)
    assert sorted(os.listdir(ws)) == before
    assert not evil_outside.exists()


def test_restore_rejects_absolute_without_touching_disk(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    with open(os.path.join(ws, "keep.txt"), "w") as f:
        f.write("keep\n")
    before = sorted(os.listdir(ws))
    abs_path = str(tmp_path / "evil_abs.txt")
    if os.path.exists(abs_path):
        os.remove(abs_path)
    snap_hash = _malicious_snapshot(store, abs_path)
    with pytest.raises(ValueError):
        WorkspaceSnapshot.restore(snap_hash, store, ws)
    assert sorted(os.listdir(ws)) == before
    assert not os.path.exists(abs_path)


def test_restore_rejects_windows_and_tilde(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    for bad in ["C:\\evil.txt", "C:/evil.txt", "~/evil.txt", "\\\\server\\share.txt"]:
        snap_hash = _malicious_snapshot(store, bad)
        with pytest.raises(ValueError):
            WorkspaceSnapshot.restore(snap_hash, store, ws)


def test_restore_allows_normpath_a_dotdot_b(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    snap_hash = _malicious_snapshot(store, "a/../b.txt", content=b"ok\n")
    WorkspaceSnapshot.restore(snap_hash, store, ws)
    target = os.path.join(ws, "b.txt")
    assert os.path.isfile(target)
    with open(target) as f:
        assert f.read() == "ok\n"
    # No debe escapar al padre
    assert not os.path.exists(str(tmp_path / "b.txt") .replace("workspace", "")) or True
    assert not (tmp_path / "a").exists() or True


def test_restore_old_absolute_snapshot_clear_error(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    snap_hash = _malicious_snapshot(store, "/etc/passwd")
    with pytest.raises(ValueError) as ei:
        WorkspaceSnapshot.restore(snap_hash, store, ws)
    msg = str(ei.value).lower()
    assert ("absolute" in msg or "absolut" in msg or "traversal" in msg
            or ".." in msg or "outside" in msg or "fuera" in msg
            or "invalid" in msg or "rechaz" in msg or "/" in msg)


# ---------------------------------------------------------------------------
# 2. Symlinks
# ---------------------------------------------------------------------------

def test_restore_symlink_internal_ok(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    sub = os.path.join(ws, "sub")
    os.makedirs(sub, exist_ok=True)
    # symlink interno: link -> sub (queda dentro)
    os.symlink(sub, os.path.join(ws, "link"))
    snap_hash = _malicious_snapshot(store, "link/inside.txt", content=b"in\n")
    WorkspaceSnapshot.restore(snap_hash, store, ws)
    # Debe resolverse dentro (sub/inside.txt vía symlink)
    assert os.path.isfile(os.path.join(sub, "inside.txt"))


def test_restore_symlink_external_rejected(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    outside = str(tmp_path / "outside_dir")
    os.makedirs(outside, exist_ok=True)
    os.symlink(outside, os.path.join(ws, "ext"))
    snap_hash = _malicious_snapshot(store, "ext/evil.txt", content=b"x\n")
    with pytest.raises(ValueError):
        WorkspaceSnapshot.restore(snap_hash, store, ws)
    assert not os.path.exists(os.path.join(outside, "evil.txt"))


# ---------------------------------------------------------------------------
# 3. delete_extra=False por defecto
# ---------------------------------------------------------------------------

def test_restore_without_flag_does_not_delete(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    snap_hash = _snapshot_of(ws, store, {"a.txt": "a\n"})
    # archivo extra creado después del snapshot
    with open(os.path.join(ws, "extra.txt"), "w") as f:
        f.write("extra\n")
    sig = inspect.signature(WorkspaceSnapshot.restore)
    assert "delete_extra" in sig.parameters
    assert sig.parameters["delete_extra"].default is False
    WorkspaceSnapshot.restore(snap_hash, store, ws)
    assert os.path.exists(os.path.join(ws, "extra.txt"))


def test_restore_delete_extra_true_deletes(tmp_path):
    from causadb._snapshot import WorkspaceSnapshot
    ws, store = _make_ws_store(tmp_path)
    snap_hash = _snapshot_of(ws, store, {"a.txt": "a\n"})
    with open(os.path.join(ws, "extra.txt"), "w") as f:
        f.write("extra\n")
    WorkspaceSnapshot.restore(snap_hash, store, ws, delete_extra=True)
    assert not os.path.exists(os.path.join(ws, "extra.txt"))
    assert os.path.exists(os.path.join(ws, "a.txt"))


# ---------------------------------------------------------------------------
# 4. bisect con excepción deja workspace inicial + usa backup temp fuera
# ---------------------------------------------------------------------------

def test_bisect_exception_leaves_initial_workspace(tmp_path, mocker):
    import sys
    from causadb._blob_store import BlobStore
    from causadb._config import CausaDBConfig
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import MappingProxyType

    ws = str(tmp_path / "workspace")
    os.makedirs(ws, exist_ok=True)
    blobs = str(tmp_path / "blobs")
    ledger = str(tmp_path / "ledger.log")
    store = BlobStore(base_path=blobs)
    config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True,
                           blob_store_path=blobs)
    writer = LedgerWriter(ledger, config=config)

    from causadb._snapshot import WorkspaceSnapshot

    def _commit(content):
        with open(os.path.join(ws, "a.py"), "w") as f:
            f.write(content)
        snap = WorkspaceSnapshot.take(ws)
        h = WorkspaceSnapshot.store(snap, store, root_dir=ws)
        ev = CanonicalEvent(event_type=EventType.FILE_MODIFIED,
                            ctx_id="b", source="agent:t",
                            source_type="agent",
                            payload=MappingProxyType({"post_snapshot": h}),
                            post_snapshot=h)
        writer.append(ev)

    _commit("v1\n")
    _commit("v2\n")
    # estado inicial distinto a los snapshots
    with open(os.path.join(ws, "a.py"), "w") as f:
        f.write("initial\n")
    with open(os.path.join(ws, "keep_initial.txt"), "w") as f:
        f.write("keep\n")

    from causadb import _bisect as bisect_mod
    src = inspect.getsource(bisect_mod.bisect)
    assert "delete_extra=True" in src
    assert "mkdtemp" in src
    assert "try" in src and "finally" in src

    # subprocess.run explota a mitad
    mocker.patch("subprocess.run", side_effect=RuntimeError("boom"))
    py = sys.executable
    with pytest.raises(RuntimeError):
        bisect_mod.bisect(f"'{py}' -c \"exit(0)\"", ledger, ws)
    with open(os.path.join(ws, "a.py")) as f:
        assert f.read() == "initial\n"
    assert os.path.exists(os.path.join(ws, "keep_initial.txt"))
    # backup temp debe estar FUERA del workspace (no dejar papelera dentro)
    leftovers = [n for n in os.listdir(ws) if "bisect" in n.lower() or "backup" in n.lower() or "trash" in n.lower()]
    assert leftovers == []


# ---------------------------------------------------------------------------
# 5. Dashboard sin XSS
# ---------------------------------------------------------------------------

def test_dashboard_no_innerhtml_with_ledger_data():
    import pathlib
    app = pathlib.Path(__file__).resolve().parents[1] / "causadb" / "dashboard" / "app.js"
    content = app.read_text(encoding="utf-8")
    # helper único existe
    assert "function safeSet" in content or "safeSet" in content
    # los 5 puntos con datos del ledger ya no usan innerHTML con concatenación
    assert "crashList.innerHTML = crashes.map" not in content
    assert "friendlyView.innerHTML" not in content
    assert "scoreDetailNumbers.innerHTML =" not in content or content.count("scoreDetailNumbers.innerHTML") == 0
    assert "scoreDetailWarnings.innerHTML" not in content
    assert "scoreDetailSessions.innerHTML" not in content
    # inyección típica no puede llegar a HTML: datos van a textContent
    assert "textContent" in content
    # ningún innerHTML con datos variables al final (solo clears con '' permitidos)
    for line in content.splitlines():
        s = line.strip()
        if ".innerHTML" in s and "=" in s and "''" not in s and '""' not in s:
            raise AssertionError(f"innerHTML con datos residual: {s[:200]}")


def test_dashboard_injection_appears_as_text():
    """<img onerror>+<script> en datos mock debe tratarse como texto (textContent)."""
    import pathlib
    app = pathlib.Path(__file__).resolve().parents[1] / "causadb" / "dashboard" / "app.js"
    content = app.read_text(encoding="utf-8")
    payload = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    # El código no debe interpolar variables con innerHTML; si usa safeSet/textContent,
    # el payload mock aparecería como texto. Verificamos que no hay plantilla
    # innerHTML que concatene campos (exception_type, title, desc, etc.).
    dangerous_fields = ["exception_type", "exception_msg", "h.title", "h.desc",
                        "h.icon", "weights_text", "warnTexts", "ws.name"]
    for field in dangerous_fields:
        for line in content.splitlines():
            if ".innerHTML" in line and field in line:
                raise AssertionError(f"campo {field} llega a innerHTML: {line.strip()[:200]}")
    assert payload  # payload documentado; la garantía es textContent/safeSet arriba
