import pytest
import os
from causadb._replay_tier import ReplayTier, ReplayStratification
from causadb._config import CausaDBConfig
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_tier_hot(ledger_path):
    config = CausaDBConfig(ledger_path=ledger_path, hot_tier_size=2)
    writer = LedgerWriter(ledger_path, config)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1); writer.append(e2)
    
    strat = ReplayStratification(config)
    assert strat.get_tier(e2.event_id, ledger_path) == ReplayTier.HOT

def test_tier_warm(ledger_path):
    # Hot tier size 1, e1 es warm, e2 es hot
    config = CausaDBConfig(ledger_path=ledger_path, hot_tier_size=1)
    writer = LedgerWriter(ledger_path, config)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1); writer.append(e2)
    
    strat = ReplayStratification(config)
    assert strat.get_tier(e1.event_id, ledger_path) == ReplayTier.WARM

def test_tier_archival(ledger_path, tmp_path):
    from causadb._archive_manager import ArchiveManager
    archive = str(tmp_path / "archive")
    os.makedirs(archive)
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent")
    writer.append(e1)
    
    manager = ArchiveManager(ledger_path, archive)
    manager.archive_current_epoch()
    
    config = CausaDBConfig(ledger_path=ledger_path)
    strat = ReplayStratification(config)
    assert strat.get_tier(e1.event_id, ledger_path) == ReplayTier.ARCHIVAL

def test_no_constitutional_tier(ledger_path):
    config = CausaDBConfig(ledger_path=ledger_path)
    strat = ReplayStratification(config)
    assert not hasattr(ReplayTier, "CONSTITUTIONAL_REPLAY")

def test_tier_N_configurable(tmp_path):
    """Artículo IX: valida default 100 + configurabilidad + efecto real."""
    path = str(tmp_path / "ledger.log")
    # Default debe ser 100
    default_cfg = CausaDBConfig(ledger_path=path)
    assert default_cfg.hot_tier_size == 100, (
        f"Default hot_tier_size debe ser 100, got {default_cfg.hot_tier_size}"
    )
    # Configurable
    cfg = CausaDBConfig(ledger_path=path, hot_tier_size=50)
    assert cfg.hot_tier_size == 50
    strat = ReplayStratification(cfg)
    assert strat.config.hot_tier_size == 50

def test_tier_unknown_event_raises(ledger_path):
    """Article IX + Fall-Closed: event_id no existente debe raise, no devolver WARM."""
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    config = CausaDBConfig(ledger_path=ledger_path, hot_tier_size=1)
    strat = ReplayStratification(config)
    with pytest.raises(ValueError, match="not found"):
        strat.get_tier("nonexistent-uuid-xyz", ledger_path)
