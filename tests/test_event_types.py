import pytest
from causadb._event_types import EventType

def test_event_type_file_modified_exists():
    assert EventType.FILE_MODIFIED

def test_event_type_command_run_exists():
    assert EventType.COMMAND_RUN

def test_event_type_commit_made_exists():
    assert EventType.COMMIT_MADE

def test_event_type_tool_called_exists():
    assert EventType.TOOL_CALLED

def test_event_type_db_query_exists():
    assert EventType.DB_QUERY

def test_event_type_config_changed_exists():
    assert EventType.CONFIG_CHANGED

def test_event_type_session_started_exists():
    assert EventType.SESSION_STARTED

def test_event_type_session_ended_exists():
    assert EventType.SESSION_ENDED

def test_event_type_working_set_resolved_absent():
    with pytest.raises(AttributeError):
        EventType.WORKING_SET_RESOLVED

def test_event_type_handoff_absent():
    with pytest.raises(AttributeError):
        EventType.HANDOFF_INITIATED

def test_event_type_llm_invoked_exists():
    assert EventType.LLM_INVOKED.value == "LLM_INVOKED"

def test_event_type_cost_accounted_exists():
    assert EventType.COST_ACCOUNTED.value == "COST_ACCOUNTED"

def test_event_type_retrieval_done_exists():
    from causadb._event_types import EventType
    assert EventType.RETRIEVAL_DONE.value == "RETRIEVAL_DONE"

def test_event_type_memory_op_exists():
    from causadb._event_types import EventType
    assert EventType.MEMORY_OP.value == "MEMORY_OP"

def test_event_type_agent_handoff_exists():
    from causadb._event_types import EventType
    assert EventType.AGENT_HANDOFF.value == "AGENT_HANDOFF"

def test_event_type_human_feedback_exists():
    from causadb._event_types import EventType
    assert EventType.HUMAN_FEEDBACK.value == "HUMAN_FEEDBACK"

def test_event_type_sandbox_state_exists():
    from causadb._event_types import EventType
    assert EventType.SANDBOX_STATE.value == "SANDBOX_STATE"

def test_event_type_reasoning_step_exists():
    from causadb._event_types import EventType
    assert EventType.REASONING_STEP.value == "REASONING_STEP"

def test_event_type_context_compacted_exists():
    from causadb._event_types import EventType
    assert EventType.CONTEXT_COMPACTED.value == "CONTEXT_COMPACTED"

def test_event_type_stream_interrupted_exists():
    from causadb._event_types import EventType
    assert EventType.STREAM_INTERRUPTED.value == "STREAM_INTERRUPTED"

def test_event_type_score_recorded_exists():
    from causadb._event_types import EventType
    assert EventType.SCORE_RECORDED.value == "SCORE_RECORDED"

def test_event_type_skill_created_exists():
    from causadb._event_types import EventType
    assert EventType.SKILL_CREATED.value == "SKILL_CREATED"

def test_event_type_skill_pruned_exists():
    from causadb._event_types import EventType
    assert EventType.SKILL_PRUNED.value == "SKILL_PRUNED"

def test_event_type_governance_decision_exists():
    from causadb._event_types import EventType
    assert EventType.GOVERNANCE_DECISION.value == "GOVERNANCE_DECISION"

def test_observation_event_type_exists():
    from causadb._event_types import EventType
    assert EventType.OBSERVATION.value == "OBSERVATION"

def test_observation_resolved_event_type_exists():
    from causadb._event_types import EventType
    assert EventType.OBSERVATION_RESOLVED.value == "OBSERVATION_RESOLVED"

def test_governance_decision_status_changed_event_type_exists():
    """R.2.1 — EventType.GOVERNANCE_DECISION_STATUS_CHANGED debe estar en el Enum."""
    from causadb._event_types import EventType
    assert EventType.GOVERNANCE_DECISION_STATUS_CHANGED.value == "GOVERNANCE_DECISION_STATUS_CHANGED"

def test_project_snapshot_event_type_exists():
    from causadb._event_types import EventType
    assert EventType.PROJECT_SNAPSHOT.value == "PROJECT_SNAPSHOT"


# ---------------------------------------------------------------------------
# F1.2 — tipos nuevos de Génesis (provenance en payload, sin tocar schema)
# ---------------------------------------------------------------------------


def test_codebase_architecture_snapshot_type_registered():
    """F1.2: CODEBASE_ARCHITECTURE_SNAPSHOT resuelve, no degrada a OBSERVATION."""
    from causadb._event_types import EventType
    assert EventType("CODEBASE_ARCHITECTURE_SNAPSHOT").value == "CODEBASE_ARCHITECTURE_SNAPSHOT"


def test_genesis_summary_type_registered():
    """F1.2: GENESIS_SUMMARY resuelve, no degrada a OBSERVATION."""
    from causadb._event_types import EventType
    assert EventType("GENESIS_SUMMARY").value == "GENESIS_SUMMARY"


def test_codebase_snapshot_event_roundtrip(tmp_path):
    """F1.2: un evento CODEBASE_ARCHITECTURE_SNAPSHOT se escribe y lee."""
    from types import MappingProxyType
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._ledger_reader import LedgerReader
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType

    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]
    writer = LedgerWriter(ledger)
    ev = CanonicalEvent(
        event_type=EventType("CODEBASE_ARCHITECTURE_SNAPSHOT"),
        ctx_id="genesis",
        source="causadb:genesis",
        payload=MappingProxyType({
            "project_id": "abc",
            "generated_at": "2026-01-01T00:00:00Z",
            "nodes": [],
            "edges": [],
            "generator": "ast",
        }),
    )
    writer.append(ev)

    events = list(LedgerReader(ledger).read_all())
    snaps = [e for e in events if e.event_type.value == "CODEBASE_ARCHITECTURE_SNAPSHOT"]
    assert len(snaps) == 1
    assert snaps[0].payload["generator"] == "ast"
    assert snaps[0].payload["project_id"] == "abc"


def test_genesis_summary_event_roundtrip(tmp_path):
    """F1.2: un evento GENESIS_SUMMARY se escribe y lee."""
    from types import MappingProxyType
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._ledger_reader import LedgerReader
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType

    ledger = causadb_init(str(tmp_path / "ws"))["ledger_path"]
    writer = LedgerWriter(ledger)
    ev = CanonicalEvent(
        event_type=EventType("GENESIS_SUMMARY"),
        ctx_id="genesis",
        source="causadb:genesis",
        payload=MappingProxyType({
            "project_id": "abc",
            "generated_at": "2026-01-01T00:00:00Z",
            "sources": {"git": 2},
            "events_imported": 2,
            "summary": "ok",
        }),
    )
    writer.append(ev)

    events = list(LedgerReader(ledger).read_all())
    sums = [e for e in events if e.event_type.value == "GENESIS_SUMMARY"]
    assert len(sums) == 1
    assert sums[0].payload["events_imported"] == 2
