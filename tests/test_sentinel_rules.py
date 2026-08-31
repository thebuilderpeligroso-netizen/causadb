import pytest
from causadb._sentinel_rules import evaluate_rules
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_evaluate_rules_no_ledger_path_raises():
    with pytest.raises(TypeError):
        evaluate_rules()

def test_all_pass(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    report = evaluate_rules(ledger_path)
    assert report.all_rules_pass

def test_hash_chain_fails(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    with open(ledger_path, "r+") as f:
        content = f.read()
        f.seek(0)
        f.write(content.replace("hash", "hosh"))
    report = evaluate_rules(ledger_path)
    assert not report.all_rules_pass
    assert "DRIFT_DETECTED" in report.summary
    # Anti-teatro: validar que ESPECIFICAMENTE hash_chain_integrity falla
    rule_map = {r.rule_name: r.passed for r in report.results}
    assert rule_map["hash_chain_integrity"] is False, (
        "hash_chain_integrity debería fallar — fue la rompida manualmente"
    )

def test_replay_fails(ledger_path):
    # Ledger con JSON inválido -> replay falla
    with open(ledger_path, "w") as f:
        f.write("{invalido}\n")
    report = evaluate_rules(ledger_path)
    assert not report.all_rules_pass

def test_causal_drift_detected(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent", parent_event_id="orphan"))
    report = evaluate_rules(ledger_path)
    assert not report.all_rules_pass

def test_correct_number_of_rules(ledger_path):
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent"))
    report = evaluate_rules(ledger_path)
    assert len(report.results) == 3

def test_evaluate_rules_no_working_set_import():
    """Anti-regresión: SentinelRules no debe importar cortex_runtime ni WorkingSet."""
    import causadb._sentinel_rules as sr
    import inspect
    src = inspect.getsource(sr)
    assert "from cortex_runtime" not in src, (
        "SentinelRules no debe importar cortex_runtime (autocontenido)"
    )
    assert "working_set.resolver" not in src, (
        "SentinelRules no debe depender de WorkingSet de Cortex (Categoría C)"
    )
    assert "from cortex_runtime_node" not in src
