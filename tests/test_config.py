import pytest
import os
from causadb._config import CausaDBConfig

def test_config_ledger_path_required():
    with pytest.raises(TypeError):
        CausaDBConfig()

def test_config_relative_ledger_path_raises():
    with pytest.raises(ValueError):
        CausaDBConfig(ledger_path="ledger.log")

def test_config_absolute_ledger_path_ok():
    cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
    assert cfg.ledger_path == "/tmp/causadb/ledger.log"

def test_config_chronicle_path_optional():
    cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
    assert cfg.chronicle_path == "/tmp/causadb/CAUSADB_CHRONICLE.md"

def test_config_redaction_default_true():
    cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
    assert cfg.redaction_enabled is True

def test_config_no_llm_providers():
    cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
    assert not hasattr(cfg, 'PROVIDER')
    assert not hasattr(cfg, 'MODEL_ID')

def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/env/ledger.log")
    cfg = CausaDBConfig.from_env()
    assert cfg.ledger_path == "/tmp/env/ledger.log"

def test_config_workspace_dir_default_none():
    """workspace_dir is never derived in __post_init__ — default stays None."""
    cfg = CausaDBConfig(ledger_path="/tmp/causadb/ledger.log")
    assert cfg.workspace_dir is None

def test_config_workspace_dir_from_env(monkeypatch):
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/env/ledger.log")
    monkeypatch.setenv("CAUSADB_WORKSPACE_DIR", "/tmp/env/workspace")
    cfg = CausaDBConfig.from_env()
    assert cfg.workspace_dir == "/tmp/env/workspace"

def test_config_workspace_dir_env_unset_defaults_none(monkeypatch):
    monkeypatch.setenv("CAUSADB_LEDGER_PATH", "/tmp/env/ledger.log")
    monkeypatch.delenv("CAUSADB_WORKSPACE_DIR", raising=False)
    cfg = CausaDBConfig.from_env()
    assert cfg.workspace_dir is None

def test_config_workspace_dir_constructor_param():
    """workspace_dir is a real dataclass field — settable via constructor."""
    cfg = CausaDBConfig(
        ledger_path="/tmp/causadb/ledger.log",
        workspace_dir="/tmp/causadb/workspace",
    )
    assert cfg.workspace_dir == "/tmp/causadb/workspace"
