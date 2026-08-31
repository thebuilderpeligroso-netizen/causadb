"""Tests para FilesystemSource — fuente de harvest de cambios de archivos.

Cobertura:
  1. test_nuevo_archivo — archivo creado → FILE_MODIFIED con action="created"
  2. test_modifica_archivo — modifica existente → nuevo evento con action="modified"
  3. test_anti_teatro_no_hardcodea — 3 archivos → 3 paths distintos
  4. test_archivo_borrado — archivo borrado → action="deleted" con timestamp determinístico

Fase 0 (sedimentación del size cap, 2026-08-05) — 3 tests nuevos:
  5. test_filesystem_large_file_metadata_only — archivo > MAX_BLOB_SIZE → metadata only
  6. test_filesystem_binary_file_metadata_only — binario (byte \x00) → metadata only
  7. test_filesystem_memory_error_metadata_only — MemoryError en read → metadata only
"""

import os
import pytest
from causadb._harvest_source_filesystem import FilesystemSource, MAX_BLOB_SIZE
from causadb._config import CausaDBConfig


def _source_with_blobs(project_root):
    """FilesystemSource con BlobStore habilitado (content_hash presente)."""
    config = CausaDBConfig(ledger_path="/fake/ledger.log", blob_store_enabled=True)
    return FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(project_root),
        config=config,
    )


def test_nuevo_archivo(tmp_path):
    """Crea un archivo en el proyecto, la fuente lo detecta como created."""
    (tmp_path / "nuevo.txt").write_text("hola")

    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(tmp_path),
    )
    assert source.detect()

    events = list(source.harvest())
    assert len(events) == 1
    assert events[0]["type"] == "FILE_MODIFIED"
    assert events[0]["action"] == "created"
    assert events[0]["path"] == "nuevo.txt"
    assert events[0]["size"] == 4


def test_modifica_archivo(tmp_path):
    """Primera cosecha detecta el archivo. Se modifica. Segunda cosecha
    detecta el cambio como action=modified. El cursor persiste entre
    llamadas."""
    (tmp_path / "nota.md").write_text("v1")
    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(tmp_path),
    )

    # Primera cosecha: created
    cursor = None
    events1 = list(source.harvest(cursor))
    assert len(events1) == 1
    assert events1[0]["action"] == "created"

    cursor = source.advance_cursor(cursor, events1)
    assert "files" in cursor
    assert "nota.md" in cursor["files"]

    # Segunda cosecha sin cambios: 0 eventos
    events2 = list(source.harvest(cursor))
    assert len(events2) == 0

    # Modificar y tercera cosecha: modified
    # Reescribimos el mismo archivo con nuevo contenido
    (tmp_path / "nota.md").write_text("v1 actualizado")
    events3 = list(source.harvest(cursor))
    assert len(events3) == 1
    assert events3[0]["action"] == "modified"
    assert events3[0]["path"] == "nota.md"

    cursor = source.advance_cursor(cursor, events3)
    assert cursor["files"]["nota.md"]["size"] > 2


def test_anti_teatro_no_hardcodea(tmp_path):
    """Crea 3 archivos distintos. Verifica que los event_ids son únicos
    (no hardcodeados)."""
    (tmp_path / "a.py").write_text("x=1")
    (tmp_path / "b.py").write_text("y=2")
    (tmp_path / "c.py").write_text("z=3")

    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(tmp_path),
    )
    events = list(source.harvest())
    assert len(events) == 3

    # event_id se calcula por SHA-256 del raw dict en el Harvester.
    # Acá verificamos que los paths y actions son distintos.
    paths = {e["path"] for e in events}
    assert paths == {"a.py", "b.py", "c.py"}
    for e in events:
        assert e["action"] == "created"
        assert e["type"] == "FILE_MODIFIED"


def test_archivo_borrado(tmp_path):
    """Crea un archivo, lo cosecha, lo borra. La siguiente cosecha debe
    producir action="deleted" con timestamp = mtime previo (determinístico,
    no time.time())."""
    f = tmp_path / "temp.txt"
    f.write_text("temporal")
    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(tmp_path),
    )

    # Primera cosecha: created
    events1 = list(source.harvest())
    assert len(events1) == 1
    assert events1[0]["action"] == "created"
    prev_mtime = events1[0]["timestamp"]

    cursor = source.advance_cursor(None, events1)

    # Borrar y segunda cosecha: deleted
    f.unlink()
    events2 = list(source.harvest(cursor))
    assert len(events2) == 1
    assert events2[0]["action"] == "deleted"
    assert events2[0]["path"] == "temp.txt"
    assert events2[0]["size"] == 0
    # Timestamp determinístico: mismo mtime previo, no time.time()
    assert events2[0]["timestamp"] == prev_mtime

    # Tercera cosecha sin el archivo: 0 eventos (ya no está en el cursor)
    cursor = source.advance_cursor(cursor, events2)
    events3 = list(source.harvest(cursor))
    assert len(events3) == 0


# ===================================================================
# Fase 0 — Size cap sedimentado (MAX_BLOB_SIZE / binarios / MemoryError)
# ===================================================================

def test_filesystem_large_file_metadata_only(tmp_path):
    """Fase 0 — archivo > MAX_BLOB_SIZE → evento con content_hash=None
    (solo metadata; nunca se carga el contenido en RAM)."""
    big = tmp_path / "big.dat"
    with open(big, "wb") as f:
        f.seek(MAX_BLOB_SIZE)  # hueco de 10MB + 1 byte
        f.write(b"x")
    assert big.stat().st_size > MAX_BLOB_SIZE

    source = _source_with_blobs(tmp_path)
    events = list(source.harvest())
    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert events[0]["size"] > MAX_BLOB_SIZE
    assert events[0]["content_hash"] is None, (
        "archivo > MAX_BLOB_SIZE → metadata only (content_hash=None)"
    )


def test_filesystem_binary_file_metadata_only(tmp_path):
    """Fase 0 — archivo binario (byte nulo \x00 en los primeros bytes) →
    metadata only (content_hash=None), no se persiste contenido."""
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"PK\x00\x00\x00\x00resto-binario")

    source = _source_with_blobs(tmp_path)
    events = list(source.harvest())
    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert events[0]["content_hash"] is None, (
        "binario (\\x00) → metadata only (content_hash=None)"
    )


# ===================================================================
# BIT-CHR.110 TD-#3b — FilesystemSource toma snapshots pre/post del batch
# ===================================================================

def test_filesystem_source_writes_pre_post_snapshots(tmp_path):
    """TD-#3b.1 — FilesystemSource.harvest() toma un snapshot pre-walk y
    otro post-walk, y cada evento FILE_MODIFIED lleva ``pre_snapshot`` y
    ``post_snapshot`` (hashes del manifest en BlobStore).

    Anti-teatro: el test muta el archivo DURANTE el harvest (entre el pre
    y el post snapshot) para verificar que el pre_snapshot captura el estado
    ANTES de la mutación y el post_snapshot captura el estado DESPUÉS —
    probando que son snapshots reales distintos, no dos iguales.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = CausaDBConfig(
        ledger_path="/fake/ledger.log",
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(workspace),
        config=config,
    )

    # Harvest 1: crea X.py con "line1\n"
    (workspace / "X.py").write_text("line1\n")
    events1 = list(source.harvest())
    cursor = source.advance_cursor(None, events1)

    # Para el harvest 2: mutamos X.py ANTES del harvest para que el cursor
    # detecte el cambio (mtime+size distintos). El pre_snapshot del harvest 2
    # captura el estado ya mutado. Para probar que pre y post son distintos,
    # mutamos X.py de nuevo DURANTE el harvest (entre pre y post) usando un
    # wrapper del walk que muta el archivo al primer yield.
    (workspace / "X.py").write_text("line1\nline2\n")

    # Wrapper del _walk que muta X.py al primer yield (simula mutación
    # durante el harvest, entre pre y post snapshot).
    original_walk = source._walk
    mutation_done = [False]

    def mutating_walk(prev_files, current_files):
        for ev in original_walk(prev_files, current_files):
            if not mutation_done[0]:
                # Mutar X.py DURANTE el harvest, después del pre_snapshot
                (workspace / "X.py").write_text("line1\nline2\nline3\n")
                # Forzar mtime change para que el post_snapshot sea distinto
                import time as _t
                new_mtime = int(_t.time()) + 100
                os.utime(str(workspace / "X.py"), (new_mtime, new_mtime))
                mutation_done[0] = True
            yield ev

    source._walk = mutating_walk

    events2 = list(source.harvest(cursor))
    assert len(events2) == 1, "debe detectar la mutación de X.py"
    ev = events2[0]
    assert ev["action"] == "modified"

    # Aserción 1: pre_snapshot y post_snapshot NO None
    assert ev.get("pre_snapshot") is not None, \
        "pre_snapshot debe estar presente (hash del manifest)"
    assert ev.get("post_snapshot") is not None, \
        "post_snapshot debe estar presente (hash del manifest)"

    # Aserción 2 y 3: resolver los manifests via BlobStore y comparar contenido
    blob_store = source._blob_store
    pre_manifest = blob_store.get(ev["pre_snapshot"])
    post_manifest = blob_store.get(ev["post_snapshot"])

    # Resolver el content blob de X.py en cada manifest
    pre_blob_ref = pre_manifest["blob_refs"][pre_manifest["files"]["X.py"]["hash"]]
    post_blob_ref = post_manifest["blob_refs"][post_manifest["files"]["X.py"]["hash"]]

    import base64
    pre_content = base64.b64decode(blob_store.get(pre_blob_ref)["content_b64"]).decode()
    post_content = base64.b64decode(blob_store.get(post_blob_ref)["content_b64"]).decode()

    # Anti-teatro: el pre tiene "line2" pero NO "line3"; el post tiene "line3"
    assert "line3" not in pre_content, (
        "pre_snapshot debe capturar el estado ANTES de la mutación durante "
        f"el harvest (sin line3). Contenido pre: {pre_content!r}"
    )
    assert "line3" in post_content, (
        "post_snapshot debe capturar el estado DESPUÉS de la mutación durante "
        f"el harvest (con line3). Contenido post: {post_content!r}"
    )


def test_filesystem_source_snapshot_dedups_content(tmp_path):
    """TD-#3b.1 — Si entre harvest 1 y harvest 2 NO se mutan archivos, los
    content blobs del snapshot se reusan (no se duplican en BlobStore).

    Anti-teatro: cuenta real de blobs en disco antes y después.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = CausaDBConfig(
        ledger_path="/fake/ledger.log",
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(workspace),
        config=config,
    )

    def _count_blobs():
        count = 0
        for root, _dirs, files in os.walk(config.blob_store_path):
            for fn in files:
                if fn.endswith(".json"):
                    count += 1
        return count

    (workspace / "a.py").write_text("content_a\n")
    (workspace / "b.py").write_text("content_b\n")

    events1 = list(source.harvest())
    cursor = source.advance_cursor(None, events1)
    blobs_after_1 = _count_blobs()
    assert blobs_after_1 > 0, "harvest 1 debe persistir blobs (manifests + content)"

    # Harvest 2 SIN mutar archivos — el cursor marca todo como unchanged,
    # así que NO se emiten eventos FILE_MODIFIED. Pero el snapshot pre/post
    # del batch vacío igual se toma. Los content blobs deben dedup.
    events2 = list(source.harvest(cursor))
    blobs_after_2 = _count_blobs()

    # Los content blobs de a.py y b.py ya existen desde harvest 1 →
    # harvest 2 no debe duplicarlos. Solo se agregan los manifests nuevos
    # (pre + post del harvest 2), que son 2 blobs extra.
    new_blobs = blobs_after_2 - blobs_after_1
    # Anti-teatro: si se duplicaran content blobs, new_blobs sería >= 4
    # (2 content + 2 manifest). Con dedup, new_blobs == 2 (solo manifests).
    assert new_blobs == 2, (
        f"harvest 2 sin mutaciones debe agregar solo 2 manifests nuevos, "
        f"no duplicar content blobs. new_blobs={new_blobs} "
        f"(after_1={blobs_after_1}, after_2={blobs_after_2})"
    )


def test_filesystem_source_snapshot_timeout_disables(tmp_path, mocker):
    """TD-#3b.1 — Si el snapshot timeout, el harvest NO falla: eventos se
    emiten con pre_snapshot=None/post_snapshot=None. Un SEGUNDO harvest
    después del timeout NO reintenta snapshotting — ``_snapshot_disabled``
    queda True permanentemente.

    Anti-teatro: assert explícito sobre ``_snapshot_disabled`` (flag interno
    del FilesystemSource, no sobre el flag del LedgerWriter).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = CausaDBConfig(
        ledger_path="/fake/ledger.log",
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(workspace),
        config=config,
    )

    # Mock WorkspaceSnapshot.take para que retorne None (simula timeout)
    from causadb._snapshot import WorkspaceSnapshot
    mocker.patch.object(WorkspaceSnapshot, "take", return_value=None)

    (workspace / "x.py").write_text("content\n")
    events1 = list(source.harvest())
    assert len(events1) == 1
    ev1 = events1[0]
    assert ev1.get("pre_snapshot") is None, (
        "timeout → pre_snapshot debe ser None"
    )
    assert ev1.get("post_snapshot") is None, (
        "timeout → post_snapshot debe ser None"
    )
    assert source._snapshot_disabled is True, (
        "timeout debe setear _snapshot_disabled=True en FilesystemSource"
    )

    # Segundo harvest: NO reintenta snapshotting
    (workspace / "y.py").write_text("content_y\n")
    events2 = list(source.harvest(source.advance_cursor(None, events1)))
    # y.py es nuevo → 1 evento
    new_events = [e for e in events2 if e["path"] == "y.py"]
    assert len(new_events) == 1
    ev2 = new_events[0]
    assert ev2.get("pre_snapshot") is None, (
        "segundo harvest después de timeout NO reintenta snapshot"
    )
    assert ev2.get("post_snapshot") is None, (
        "segundo harvest después de timeout NO reintenta snapshot"
    )
    assert source._snapshot_disabled is True, (
        "_snapshot_disabled debe quedar True permanentemente"
    )


def test_filesystem_memory_error_metadata_only(tmp_path, monkeypatch):
    """Fase 0 — MemoryError al leer el contenido → metadata only
    (content_hash=None), el harvest no crashea."""
    f = tmp_path / "texto.txt"
    f.write_bytes(b"contenido de prueba")

    import builtins
    real_open = builtins.open

    class _MemoryErrorReader:
        """Wrapper de open() cuyo read() lanza MemoryError (OOM simulado)."""

        def __init__(self, *args, **kwargs):
            self._inner = real_open(*args, **kwargs)

        def read(self, *a, **kw):
            raise MemoryError("OOM simulado")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

    monkeypatch.setattr(builtins, "open", _MemoryErrorReader)

    source = _source_with_blobs(tmp_path)
    events = list(source.harvest())  # no debe crashear
    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert events[0]["content_hash"] is None, (
        "MemoryError → metadata only (content_hash=None)"
    )