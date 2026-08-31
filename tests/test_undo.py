"""Tests for Phase 8 — Workspace Self-Protection.

Tests:
  1. test_blob_store_default_activado — CausaDBConfig() has blob_store_enabled=True
  2. test_filesystem_source_captura_content_hash — FilesystemSource with BlobStore
     persists content_hash in raw event
  3. test_undo_restaura_archivo_desde_blob — integration: write file, harvest with
     BlobStore ON, modify file, undo, verify restored content
  4. test_undo_sin_snapshot_reporta_error — file without snapshot in ledger, returns error
  5. test_undo_archivo_no_existia — file never modified according to ledger
  6. test_cli_undo_flag_no_file_error — without --file, argparse error
  7. test_anti_teatro_undo_no_hardcodea — undo restores actual content, not hardcoded string
"""

import json
import os
import pytest
import hashlib
from unittest.mock import MagicMock, patch


# ============================================================================
# 8.1 — BlobStore enabled by default
# ============================================================================

def test_blob_store_default_activado():
    """Verifica que CausaDBConfig() tiene blob_store_enabled=True por defecto."""
    from causadb._config import CausaDBConfig
    config = CausaDBConfig(ledger_path="/tmp/test_ledger.log")
    assert config.blob_store_enabled is True, (
        "blob_store_enabled debe ser True por defecto (Fase 8.1)"
    )


def test_blob_store_from_env_default_true():
    """Verifica que from_env() usa 'true' como default para blob_store_enabled."""
    from causadb._config import CausaDBConfig
    # Sin la variable de entorno, debe ser True
    with patch.dict(os.environ, {}, clear=True):
        os.environ["CAUSADB_LEDGER_PATH"] = "/tmp/test_ledger.log"
        config = CausaDBConfig.from_env()
        assert config.blob_store_enabled is True, (
            "from_env() debe usar 'true' como default para blob_store_enabled"
        )


# ============================================================================
# 8.3 — FilesystemSource captures content_hash via BlobStore
# ============================================================================

def test_filesystem_source_captura_content_hash(tmp_path):
    """FilesystemSource con BlobStore persiste content_hash en el raw event."""
    from causadb._config import CausaDBConfig
    from causadb._harvest_source_filesystem import FilesystemSource

    ledger_path = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    config = CausaDBConfig(
        ledger_path=ledger_path,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )

    # Crear un archivo en el proyecto
    project_root = tmp_path / "project"
    project_root.mkdir()
    test_file = project_root / "test.txt"
    test_file.write_text("contenido de prueba")

    source = FilesystemSource(
        ledger_path=ledger_path,
        project_root=str(project_root),
        config=config,
    )

    events = list(source.harvest())
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "FILE_MODIFIED"
    assert event["action"] == "created"
    assert event["path"] == "test.txt"

    # Debe tener content_hash
    assert "content_hash" in event, (
        "El raw event debe incluir content_hash cuando BlobStore está activo"
    )
    assert event["content_hash"] is not None

    # Verificar que el content_hash es un SHA-256 válido (64 hex chars)
    assert len(event["content_hash"]) == 64
    assert all(c in "0123456789abcdef" for c in event["content_hash"])

    # Verificar que el blob fue persistido (usando BlobStore.get)
    from causadb._blob_store import BlobStore
    content_hash = event["content_hash"]
    blob_store = BlobStore(blob_path)
    # El content_hash en el evento es SHA-256 del contenido del archivo.
    # BlobStore.put() genera su propio hash (SHA-256 del JSON serializado).
    # Para verificar que el blob existe, buscamos en el directorio de blobs.
    blob_dir = blob_path
    assert os.path.exists(blob_dir), "El directorio de blobs debe existir"
    # Debe haber al menos un archivo de blob
    blob_files = []
    for root, dirs, files in os.walk(blob_dir):
        blob_files.extend(files)
    assert len(blob_files) >= 1, (
        f"Debe haber al menos 1 blob en {blob_dir}, encontrados: {blob_files}"
    )

    # Verificar que el contenido del blob es correcto (recuperar vía BlobStore)
    # El BlobStore usa su propio hash, no el content_hash del evento.
    # Recorremos los blobs para encontrar el que contiene nuestro contenido.
    found = False
    for blob_file_name in blob_files:
        # Reconstruir el hash completo desde la ruta
        for root2, dirs2, files2 in os.walk(blob_dir):
            for f in files2:
                if f.endswith(".json"):
                    blob_hash = f.replace(".json", "")
                    try:
                        blob_data = blob_store.get(blob_hash)
                        if blob_data.get("path") == "test.txt":
                            assert blob_data["content"] == "contenido de prueba"
                            found = True
                            break
                    except Exception:
                        continue
            if found:
                break
    assert found, "Debe existir un blob con path='test.txt' y content='contenido de prueba'"


def test_filesystem_source_content_hash_null_on_delete(tmp_path):
    """FilesystemSource con BlobStore: content_hash es null para deleted."""
    from causadb._config import CausaDBConfig
    from causadb._harvest_source_filesystem import FilesystemSource

    ledger_path = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    config = CausaDBConfig(
        ledger_path=ledger_path,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    test_file = project_root / "temp.txt"
    test_file.write_text("temporal")

    source = FilesystemSource(
        ledger_path=ledger_path,
        project_root=str(project_root),
        config=config,
    )

    # Primera cosecha: created
    events1 = list(source.harvest())
    assert len(events1) == 1
    assert events1[0]["action"] == "created"
    assert events1[0]["content_hash"] is not None

    cursor = source.advance_cursor(None, events1)

    # Borrar archivo
    test_file.unlink()

    # Segunda cosecha: deleted
    events2 = list(source.harvest(cursor))
    assert len(events2) == 1
    assert events2[0]["action"] == "deleted"
    assert events2[0]["content_hash"] is None, (
        "content_hash debe ser None para archivos deleted"
    )


def test_filesystem_source_sin_blobstore_no_content_hash(tmp_path):
    """FilesystemSource sin BlobStore NO incluye content_hash."""
    from causadb._config import CausaDBConfig
    from causadb._harvest_source_filesystem import FilesystemSource

    ledger_path = str(tmp_path / "ledger.log")
    config = CausaDBConfig(
        ledger_path=ledger_path,
        blob_store_enabled=False,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "test.txt").write_text("contenido")

    source = FilesystemSource(
        ledger_path=ledger_path,
        project_root=str(project_root),
        config=config,
    )

    events = list(source.harvest())
    assert len(events) == 1
    # Sin BlobStore, no debe haber content_hash
    assert "content_hash" not in events[0], (
        "Sin BlobStore, el evento no debe incluir content_hash"
    )


# ============================================================================
# 8.4 — causadb undo --file <path>
# ============================================================================

def test_undo_restaura_archivo_desde_blob(tmp_path):
    """Integración: write archivo, trigger harvest con BlobStore ON,
    modificar archivo, undo, verificar contenido restaurado."""
    from causadb._config import CausaDBConfig
    from causadb._harvest_source_filesystem import FilesystemSource
    from causadb._harvester import Harvester
    from causadb.cli._cmd_undo import cmd_undo

    ledger_path = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = CausaDBConfig(
        ledger_path=ledger_path,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )

    # Crear archivo original
    test_file = project_root / "test.txt"
    original_content = "contenido original v1"
    test_file.write_text(original_content)

    # Harvest con BlobStore
    source = FilesystemSource(
        ledger_path=ledger_path,
        project_root=str(project_root),
        config=config,
    )
    events = list(source.harvest())
    assert len(events) == 1
    content_hash_v1 = events[0]["content_hash"]
    assert content_hash_v1 is not None

    # Escribir eventos al ledger via Harvester
    harvester = Harvester(ledger_path)
    harvester.register_source(source)
    harvester.harvest_all(dry_run=False)

    # Modificar archivo
    test_file.write_text("contenido modificado v2")

    # Harvest de nuevo (para tener el evento de modificación)
    cursor = source.advance_cursor(None, events)
    events2 = list(source.harvest(cursor))
    harvester.write_events([
        harvester._event_from_raw("filesystem", e)
        for e in events2
    ])

    # Ahora undo: restaurar desde el último snapshot
    from types import SimpleNamespace
    args = SimpleNamespace(
        file=str(test_file),
        ledger=ledger_path,
    )
    code, output = cmd_undo(args)
    assert code == 0, f"undo debería retornar 0, retornó {code}: {output}"

    # Verificar que el archivo fue restaurado
    restored_content = test_file.read_text()
    assert restored_content == original_content, (
        f"Esperado '{original_content}', obtenido '{restored_content}'"
    )


def test_undo_restores_from_file_modified_snapshot(tmp_path):
    """B.2 / WARN-2 — undo falls back to the ``pre_snapshot`` of FILE_MODIFIED
    events when the blob-store content_hash lookup fails.

    Today this path crashes: ``_cmd_undo.py:88`` calls
    ``_find_content_in_snapshots(ledger_path, config, file_path)`` (3 args)
    but the function is defined with 2 — TypeError. And even with the
    signature fixed, the fallback only looked at PROJECT_SNAPSHOT events,
    never at the pre/post_snapshot fields the auto-snapshot writes on
    FILE_MODIFIED events. This test pins the full fix.
    """
    from causadb._blob_store import BlobStore
    from causadb._config import CausaDBConfig
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._ledger_writer import LedgerWriter
    from causadb._snapshot import WorkspaceSnapshot
    from causadb.cli._cmd_undo import cmd_undo
    from types import SimpleNamespace, MappingProxyType

    ledger = str(tmp_path / "ledger.log")
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)
    blob_path = str(tmp_path / "blobs")
    store = BlobStore(blob_path)

    target = os.path.join(workspace, "doc.txt")
    with open(target, "w") as f:
        f.write("v1 original\n")

    writer = LedgerWriter(ledger, config=CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    ))

    # Event 1: the file is created. No content_hash → the blob-store lookup
    # in cmd_undo finds nothing and the snapshot fallback must kick in.
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="agent",
        source="causadb:test",
        source_type="agent",
        payload=MappingProxyType({"action": "created", "path": "doc.txt"}),
    ))

    # pre_snapshot of the LAST event captures the state before the final
    # modification — the state undo must restore to (v1).
    pre_snap = WorkspaceSnapshot.take(workspace)
    pre_hash = WorkspaceSnapshot.store(pre_snap, store, root_dir=workspace)

    with open(target, "w") as f:
        f.write("v2 modified\n")
    post_snap = WorkspaceSnapshot.take(workspace, prev_snapshot=pre_snap)
    post_hash = WorkspaceSnapshot.store(post_snap, store, root_dir=workspace)

    # Event 2: the modification, carrying pre/post snapshots exactly like
    # the Vigilante / LedgerWriter auto-snapshot writes them.
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="agent",
        source="causadb:test",
        source_type="agent",
        payload=MappingProxyType({"action": "modified", "path": "doc.txt"}),
        pre_snapshot=pre_hash,
        post_snapshot=post_hash,
    ))

    args = SimpleNamespace(file=target, ledger=ledger)
    code, output = cmd_undo(args)
    assert code == 0, f"undo must restore via FILE_MODIFIED snapshot: {output}"
    with open(target) as f:
        restored = f.read()
    assert restored == "v1 original\n", (
        f"undo must restore the pre_snapshot content of the last event, "
        f"got: {restored!r}"
    )


def test_undo_sin_snapshot_reporta_error(tmp_path):
    """Archivo sin snapshot en ledger → retorna error."""
    from causadb.cli._cmd_undo import cmd_undo
    from types import SimpleNamespace

    ledger_path = str(tmp_path / "ledger.log")
    # Crear un ledger vacío (solo genesis)
    from causadb._ledger_writer import LedgerWriter
    writer = LedgerWriter(ledger_path)
    # No escribimos ningún FILE_MODIFIED

    args = SimpleNamespace(
        file="/tmp/nonexistent.txt",
        ledger=ledger_path,
    )
    exit_code, output = cmd_undo(args)
    assert exit_code == 1, (
        f"Debe retornar error (1) para archivo sin snapshot, retornó {exit_code}: {output}"
    )
    assert "no file_modified" in output.lower() or "no snapshot" in output.lower() or "not found" in output.lower(), (
        f"Debe reportar error de archivo no encontrado, output: {output}"
    )


def test_undo_archivo_no_existia(tmp_path):
    """Archivo que nunca fue modificado según ledger → retorna error."""
    from causadb.cli._cmd_undo import cmd_undo
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from types import SimpleNamespace, MappingProxyType

    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)

    # Escribir un FILE_MODIFIED para otro archivo, no para el que pedimos
    event = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "path": "otro_archivo.py",
            "action": "created",
            "size": 100,
        }),
    )
    writer.append(event)

    args = SimpleNamespace(
        file="/tmp/archivo_que_nunca_existio.txt",
        ledger=ledger_path,
    )
    exit_code, output = cmd_undo(args)
    assert exit_code == 1, (
        f"Debe retornar error (1) para archivo sin eventos, retornó {exit_code}: {output}"
    )


def test_cli_undo_flag_no_file_error():
    """Sin --file, argparse debe lanzar error."""
    from causadb.cli.main import build_parser
    import sys

    parser = build_parser()
    try:
        parser.parse_args(["undo"])
        assert False, "Debe lanzar SystemExit por falta de --file"
    except SystemExit:
        pass  # Esperado: argparse requiere --file


def test_anti_teatro_undo_no_hardcodea(tmp_path):
    """Verifica que undo restaura contenido real, no un string hardcodeado."""
    from causadb._config import CausaDBConfig
    from causadb._harvest_source_filesystem import FilesystemSource
    from causadb._harvester import Harvester
    from causadb.cli._cmd_undo import cmd_undo
    from types import SimpleNamespace

    ledger_path = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = CausaDBConfig(
        ledger_path=ledger_path,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )

    # Contenido único y verificable
    unique_content = "ANTI_TEATRO_" + hashlib.sha256(b"unique").hexdigest()[:16]
    test_file = project_root / "unique.txt"
    test_file.write_text(unique_content)

    source = FilesystemSource(
        ledger_path=ledger_path,
        project_root=str(project_root),
        config=config,
    )
    events = list(source.harvest())
    assert len(events) == 1

    harvester = Harvester(ledger_path)
    harvester.register_source(source)
    harvester.harvest_all(dry_run=False)

    # Modificar
    test_file.write_text("contenido diferente")

    # Harvest de nuevo
    cursor = source.advance_cursor(None, events)
    events2 = list(source.harvest(cursor))
    harvester.write_events([
        harvester._event_from_raw("filesystem", e)
        for e in events2
    ])

    # Undo
    args = SimpleNamespace(
        file=str(test_file),
        ledger=ledger_path,
    )
    exit_code, output = cmd_undo(args)
    assert exit_code == 0

    restored = test_file.read_text()
    assert restored == unique_content, (
        f"Anti-teatro: esperado '{unique_content}', obtenido '{restored}'"
    )
    # Verificar que NO es un string hardcodeado como "token1 token2"
    assert restored != "token1 token2", "Anti-teatro: no debe ser string hardcodeado"


# ============================================================================
# 8.2 — Daemon auto-start en setup
# ============================================================================

def test_setup_daemon_step_called(monkeypatch):
    """Verifica que setup incluye el step de daemon entre step 4 y 5."""
    from causadb.cli._cmd_setup import cmd_setup
    from unittest.mock import MagicMock

    install_called = []
    start_called = []

    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.init",
        lambda p: {"ledger_path": "/tmp/.causadb/ledger.log"}
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.discover",
        lambda p: "/tmp/.causadb/config.json"
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.load",
        lambda p: MagicMock(ledger_path="/tmp/.causadb/ledger.log")
    )
    monkeypatch.setattr(
        "causadb._shell_hook.install",
        lambda ctx_id: True
    )
    monkeypatch.setattr(
        "causadb._git_hook.install_post_commit_hook",
        lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "causadb._git_hook.git_dir_from_workspace",
        lambda p: "/tmp/.git"
    )
    # Patch cmd_watch en el módulo real (importado por _cmd_setup)
    monkeypatch.setattr(
        "causadb.cli._cmd_watch.cmd_watch",
        lambda a: (0, '{"vigilante": "started"}')
    )
    monkeypatch.setattr(
        "causadb._daemon_service.install_service",
        lambda ledger_path: install_called.append(ledger_path) or (True, "/tmp/causadb.service")
    )
    monkeypatch.setattr(
        "causadb._daemon_service.start_service",
        lambda: start_called.append(True) or (True, "active")
    )

    args = MagicMock()
    args.project_dir = None
    args.no_hook = False
    args.no_git = False
    args.no_watch = False
    args.integrations = None
    args.no_daemon = False

    code, output = cmd_setup(args)
    data = json.loads(output)

    # El step daemon debe existir
    assert "daemon" in data["steps"], (
        f"El step 'daemon' debe estar en los steps. Steps: {list(data['steps'].keys())}"
    )
    assert data["steps"]["daemon"]["status"] == "ok", (
        f"El step daemon debe ser 'ok', fue: {data['steps']['daemon']}"
    )
    assert len(install_called) == 1
    assert len(start_called) == 1


def test_setup_no_daemon_flag(monkeypatch):
    """Con --no-daemon, el step de daemon debe ser skipped."""
    from causadb.cli._cmd_setup import cmd_setup
    from unittest.mock import MagicMock

    install_called = []
    start_called = []

    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.init",
        lambda p: {"ledger_path": "/tmp/.causadb/ledger.log"}
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.discover",
        lambda p: "/tmp/.causadb/config.json"
    )
    monkeypatch.setattr(
        "causadb._workspace.WorkspaceManager.load",
        lambda p: MagicMock(ledger_path="/tmp/.causadb/ledger.log")
    )
    monkeypatch.setattr(
        "causadb._shell_hook.install",
        lambda ctx_id: True
    )
    monkeypatch.setattr(
        "causadb._git_hook.install_post_commit_hook",
        lambda *a, **kw: True
    )
    monkeypatch.setattr(
        "causadb._git_hook.git_dir_from_workspace",
        lambda p: "/tmp/.git"
    )
    # Patch cmd_watch en el módulo real (importado por _cmd_setup)
    monkeypatch.setattr(
        "causadb.cli._cmd_watch.cmd_watch",
        lambda a: (0, '{"vigilante": "started"}')
    )
    monkeypatch.setattr(
        "causadb._daemon_service.install_service",
        lambda ledger_path: install_called.append(ledger_path) or (True, "/tmp/causadb.service")
    )
    monkeypatch.setattr(
        "causadb._daemon_service.start_service",
        lambda: start_called.append(True) or (True, "active")
    )

    args = MagicMock()
    args.project_dir = None
    args.no_hook = False
    args.no_git = False
    args.no_watch = False
    args.integrations = None
    args.no_daemon = True

    code, output = cmd_setup(args)
    data = json.loads(output)

    assert "daemon" in data["steps"]
    assert data["steps"]["daemon"]["status"] == "skipped", (
        f"Con --no-daemon, el step debe ser 'skipped'. Fue: {data['steps']['daemon']}"
    )
    assert len(install_called) == 0
    assert len(start_called) == 0

# =============================================================================
# DEUDA-UNDO — dual-path matching + timestamp ordering
# =============================================================================

def _write_file_events_with_paths(tmp_path, path_prefixes, versions):
    """Escribe eventos FILE_MODIFIED para un archivo bajo múltiples paths
    (dual-path bug: mismo archivo trackeado como `tests/x.py` y
    `causadb/tests/x.py`). Almacena el contenido en el BlobStore (como el
    harvester real). Retorna (ledger, archivo_real, versión_esperada).
    """
    from causadb._blob_store import BlobStore
    from causadb._config import CausaDBConfig
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._ledger_writer import LedgerWriter

    ledger = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)

    store = BlobStore(blob_path)
    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )
    writer = LedgerWriter(ledger, config=config)

    real_file = os.path.join(workspace, "proj.py")
    for i, (prefix, content) in enumerate(zip(path_prefixes, versions)):
        ev_path = f"{prefix}/proj.py" if prefix else "proj.py"
        # Almacenar contenido como blob (como el harvester real)
        blob_hash = store.put({"content": content, "path": ev_path})
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="harvester:filesystem",
            source="harvester:filesystem",
            payload={
                "action": "created" if i == 0 else "modified",
                "path": ev_path,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            },
        ))
    return ledger, real_file, versions[-2]


def test_undo_restaura_con_paths_prefixed_y_relativo(tmp_path):
    """DEUDA-UNDO: undo con path relativo debe restaurar el estado correcto
    aunque el ledger tenga el archivo bajo múltiples paths (prefixed).
    """
    from causadb.cli._cmd_undo import cmd_undo
    from types import SimpleNamespace

    # Eventos viejos bajo 'tests/' (no-prefixed) y recientes bajo
    # 'causadb/tests/' (prefixed). El undo debe restaurar el estado
    # reciente (v3 recent), no los viejos.
    ledger, real_file, expected = _write_file_events_with_paths(
        tmp_path,
        ["tests", "tests", "causadb/tests", "causadb/tests"],
        ["v1 old", "v2 old", "v3 recent", "v4 current"],
    )

    args = SimpleNamespace(file=real_file, ledger=ledger)
    code, output = cmd_undo(args)
    assert code == 0, f"undo falló con dual-path: {output}"
    assert "restored_from" in output
    # El archivo debe restaurarse a v2 (penúltimo), no a v1 (el más viejo)
    with open(real_file) as fh:
        restored = fh.read()
    assert restored == "v3 recent", (
        f"Con dual-path, undo debe restaurar la versión reciente, no la vieja. "
        f"Obtenido: {restored!r}"
    )


def test_undo_saltea_eventos_deleted_al_buscar_snapshot(tmp_path):
    """DEUDA-UNDO: al buscar el 'last known good', debe saltar eventos
    'deleted' y seleccionar el primer evento con contenido real.
    """
    from causadb.cli._cmd_undo import _find_file_events

    ledger, real_file, _ = _write_file_events_with_paths(
        tmp_path,
        ["causadb/tests", "causadb/tests"],
        ["v1", "v2"],
    )
    # Agregar un evento 'deleted' al final
    from causadb._config import CausaDBConfig
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._ledger_writer import LedgerWriter
    writer = LedgerWriter(ledger, config=CausaDBConfig(ledger_path=ledger, blob_store_enabled=True))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="harvester:filesystem",
        source="harvester:filesystem",
        payload={"action": "deleted", "path": "causadb/tests/proj.py"},
    ))

    events = _find_file_events(ledger, "proj.py")
    # Los eventos deben estar ordenados por timestamp y el deleted incluido
    assert len(events) >= 3
    # El último evento debe ser el deleted
    assert events[-1]["action"] == "deleted"
