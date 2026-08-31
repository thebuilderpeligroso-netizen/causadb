import pytest
import os
import gzip
import json
import hashlib
from causadb._archive_manager import ArchiveManager
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_validator import LedgerValidator, ReplayIntegrityError

@pytest.fixture
def workspace(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    archive = str(tmp_path / "archive")
    os.makedirs(archive)
    return ledger, archive

def test_archive_current_epoch_creates_gz(workspace):
    ledger, archive = workspace
    writer = LedgerWriter(ledger)
    for _ in range(5):
        writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    
    manager = ArchiveManager(ledger, archive)
    manager.archive_current_epoch()
    
    archives = os.listdir(archive)
    assert len(archives) == 1
    assert archives[0].endswith(".gz")

def test_archive_validates_post_archiving(workspace, mocker):
    """Article IX: debe validar que archive_current_epoch llama
    LedgerValidator.validate_or_raise() ANTES de archivar."""
    ledger, archive = workspace
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    
    spy = mocker.spy(LedgerValidator, "validate_or_raise")
    
    manager = ArchiveManager(ledger, archive)
    manager.archive_current_epoch()
    
    assert spy.call_count >= 1, (
        "archive_current_epoch debe llamar LedgerValidator.validate_or_raise() "
        "antes de archivar (Fall-Closed sobre integridad del archive)"
    )

def test_archive_does_not_create_gz_if_validation_fails(workspace, mocker):
    """Article IX: si validate_or_raise falla, archive_current_epoch
    NO debe crear el .gz ni vaciar el activo (Fall-Closed real)."""
    ledger, archive = workspace
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    
    mocker.patch.object(LedgerValidator, "validate_or_raise", side_effect=ReplayIntegrityError("corrupt"))
    
    manager = ArchiveManager(ledger, archive)
    with pytest.raises(ReplayIntegrityError):
        manager.archive_current_epoch()
    
    assert os.listdir(archive) == [], (
        "Si la validación falla, no debe crearse el .gz"
    )
    assert os.path.getsize(ledger) > 0, (
        "Si la validación falla, el ledger activo no debe vaciarse"
    )

def test_archive_fsyncs_before_clearing(workspace, mocker):
    """Article IX: el .gz y last_hash.json deben fsync-arse ANTES de
    vaciar el ledger activo. Si hay crash entre ambos, no se pierde el epoch."""
    ledger, archive = workspace
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    
    fsync_targets = []
    def tracking_fsync(fd):
        try:
            path = os.readlink(f"/proc/self/fd/{fd}")
            fsync_targets.append(path)
        except (OSError, IOError):
            fsync_targets.append(f"fd:{fd}")
        return 0
    
    mocker.patch("causadb._archive_manager.os.fsync", side_effect=tracking_fsync)
    
    manager = ArchiveManager(ledger, archive)
    manager.archive_current_epoch()
    
    # Debe haber al menos 2 fsyncs: el del last_hash.json y el del activo vaciado
    assert len(fsync_targets) >= 2, (
        f"Debe haber >=2 fsyncs (last_hash.json + activo). Got {fsync_targets}"
    )

def test_new_epoch_starts_with_last_archive_hash(workspace):
    ledger, archive = workspace
    writer = LedgerWriter(ledger)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1)
    last_h = writer.last_hash
    
    manager = ArchiveManager(ledger, archive)
    manager.archive_current_epoch()
    
    # Nuevo evento debería tener prev_hash = last_h
    writer2 = LedgerWriter(ledger)
    e2 = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent")
    writer2.append(e2)
    
    with open(ledger, "r") as f:
        entry = json.loads(f.readline())
        assert entry["prev_hash"] == last_h

def test_archive_dir_configurable(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    archive = str(tmp_path / "custom_archive")
    manager = ArchiveManager(ledger, archive)
    assert manager.archive_dir == archive
