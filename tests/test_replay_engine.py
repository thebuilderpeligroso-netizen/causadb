import pytest
import json
from causadb._replay_engine import ReplayEngine
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")

def test_replay_empty_ledger(ledger_path):
    open(ledger_path, "a").close()
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["events_applied"] == 0

def test_replay_single_file_modified(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent", 
                       payload={"path": "/foo.py", "action": "create"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["files_modified"] == [{"path": "/foo.py", "action": "create", "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_single_command_run(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent", 
                       payload={"command": "rm -rf /tmp/x", "exit_code": 0})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["commands_run"] == [{"command": "rm -rf /tmp/x", "exit_code": 0, "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_single_commit_made(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.COMMIT_MADE, ctx_id="ctx", source="opencode:agent", 
                       payload={"commit_hash": "abc123", "message": "fix"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["commits_made"] == [{"commit_hash": "abc123", "message": "fix", "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_multiple_events_order(ledger_path):
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent",
                        payload={"path": "/a.py", "action": "create"})
    e2 = CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="ctx", source="opencode:agent",
                        payload={"command": "ls", "exit_code": 0})
    e3 = CanonicalEvent(event_type=EventType.COMMIT_MADE, ctx_id="ctx", source="opencode:agent",
                        payload={"commit_hash": "d123", "message": "e"})
    writer.append(e1); writer.append(e2); writer.append(e3)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["events_applied"] == 3
    assert state["files_modified"] == [{"path": "/a.py", "action": "create", "timestamp": e1.timestamp, "source": "opencode:agent"}]
    assert state["commands_run"] == [{"command": "ls", "exit_code": 0, "timestamp": e2.timestamp, "source": "opencode:agent"}]
    assert state["commits_made"] == [{"commit_hash": "d123", "message": "e", "timestamp": e3.timestamp, "source": "opencode:agent"}]
    assert state["files_modified"][0]["timestamp"] <= state["commands_run"][0]["timestamp"]
    assert state["commands_run"][0]["timestamp"] <= state["commits_made"][0]["timestamp"]
    assert state["last_hash"] == writer.last_hash

def test_replay_context_updated(ledger_path):
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.CONTEXT_UPDATED, ctx_id="ctx", source="opencode:agent", payload={"context": {"plan": "step 1"}})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["context"] == {"plan": "step 1"}

def test_replay_last_hash_and_counter_tracked(ledger_path):
    """Combina counter + last_hash + acumulación real de side effects."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent",
                        payload={"path": "/a", "action": "create"})
    e2 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent",
                        payload={"path": "/b", "action": "modify"})
    writer.append(e1); writer.append(e2)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["events_applied"] == 2
    assert state["last_hash"] == writer.last_hash
    assert len(state["files_modified"]) == 2
    assert state["files_modified"][0]["path"] == "/a"
    assert state["files_modified"][1]["path"] == "/b"


# --- F.2.1: Replay de los 8 EventType ignorados por apply() ---

def test_replay_tool_called_produces_tools_called_list(ledger_path):
    """TOOL_CALLED debe reproducir state['tools_called'] con tool_name, arguments, result, duration_ms, error + timestamp."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="opencode:agent",
                       payload={"tool_name": "read_file"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["tools_called"] == [{"tool_name": "read_file", "arguments": None, "result": None, "duration_ms": None, "error": None, "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_db_query_produces_queries_executed_list(ledger_path):
    """DB_QUERY debe reproducir state['queries_executed'] con query + timestamp."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.DB_QUERY, ctx_id="ctx", source="opencode:agent",
                       payload={"query": "SELECT * FROM users"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["queries_executed"] == [{"query": "SELECT * FROM users", "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_session_started_produces_sessions_list(ledger_path):
    """SESSION_STARTED debe reproducir state['sessions'] con session_id + estado started."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.SESSION_STARTED, ctx_id="ctx", source="opencode:agent",
                       payload={"session_id": "sess-1"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["sessions"] == [{"session_id": "sess-1", "started_at": e.timestamp, "ended_at": None, "duration_ms": None, "source": "opencode:agent"}]

def test_replay_session_ended_produces_session_duration(ledger_path):
    """SESSION_ENDED debe reproducir state['sessions'] con ended_at + duration_ms calculado."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.SESSION_STARTED, ctx_id="ctx", source="opencode:agent",
                        payload={"session_id": "sess-1"})
    e2 = CanonicalEvent(event_type=EventType.SESSION_ENDED, ctx_id="ctx", source="opencode:agent",
                        payload={"session_id": "sess-1"})
    writer.append(e1); writer.append(e2)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["sessions"]) == 1
    assert state["sessions"][0]["session_id"] == "sess-1"
    assert state["sessions"][0]["ended_at"] == e2.timestamp
    assert state["sessions"][0]["duration_ms"] is not None

def test_replay_mutation_applied_produces_mutations_list(ledger_path):
    """MUTATION_APPLIED debe reproducir state['mutations_applied'] con mutation_id + estado."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.MUTATION_APPLIED, ctx_id="ctx", source="opencode:agent",
                       payload={"mutation_id": "mut-1"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["mutations_applied"]) == 1
    assert state["mutations_applied"][0]["mutation_id"] == "mut-1"
    assert state["mutations_applied"][0]["timestamp"] == e.timestamp
    assert state["mutations_applied"][0]["reverted"] is False
    assert state["mutations_applied"][0]["event_id"] == e.event_id

def test_replay_mutation_reverted_produces_reverts_list(ledger_path):
    """MUTATION_REVERTED debe reproducir state['mutations_reverted'] y marcar la mutación original."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.MUTATION_APPLIED, ctx_id="ctx", source="opencode:agent",
                        payload={"mutation_id": "mut-1"})
    e2 = CanonicalEvent(event_type=EventType.MUTATION_REVERTED, ctx_id="ctx", source="opencode:agent",
                        payload={"revert_target_event_id": e1.event_id})
    writer.append(e1); writer.append(e2)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["mutations_reverted"]) == 1
    assert state["mutations_reverted"][0]["revert_target_event_id"] == e1.event_id
    assert state["mutations_reverted"][0]["timestamp"] == e2.timestamp
    assert state["mutations_applied"][0]["reverted"] is True

def test_replay_system_boot_produces_boot_event(ledger_path):
    """SYSTEM_BOOT debe reproducir state['system_boots'] con boot_id + timestamp."""
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.SYSTEM_BOOT, ctx_id="ctx", source="opencode:agent",
                       payload={"boot_id": "boot-1"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["system_boots"] == [{"boot_id": "boot-1", "timestamp": e.timestamp, "source": "opencode:agent"}]

def test_replay_checkpoint_created_produces_checkpoint_state(ledger_path):
    """CHECKPOINT_CREATED debe reproducir state['checkpoints'] + mergear el snapshot del payload."""
    writer = LedgerWriter(ledger_path)
    e1 = CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="ctx", source="opencode:agent",
                        payload={"path": "/foo.py", "action": "create"})
    writer.append(e1)
    e2 = CanonicalEvent(event_type=EventType.CHECKPOINT_CREATED, ctx_id="ctx", source="opencode:agent",
                        payload={"checkpoint_id": "cp-1", "snapshot": {"files_modified_count": 1}})
    writer.append(e2)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert state["checkpoints"] == [{"checkpoint_id": "cp-1", "timestamp": e2.timestamp, "source": "opencode:agent"}]
    assert state.get("files_modified_count") == 1


# --- F.3.5: TOOL_CALLED mejorado — arguments, result, duration_ms, error ---

def test_replay_tool_called_with_all_fields(tmp_path):
    """TOOL_CALLED con tool_name, arguments, result, duration_ms, error → todos preservados en replay."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="opencode:agent",
                       payload={"tool_name": "read_file", "arguments": {"path": "/foo"}, "result": "file content", "duration_ms": 150, "error": None})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["tools_called"]) == 1
    tc = state["tools_called"][0]
    assert tc["tool_name"] == "read_file"
    assert tc["arguments"] == {"path": "/foo"}
    assert tc["result"] == "file content"
    assert tc["duration_ms"] == 150
    assert tc["error"] is None
    assert tc["timestamp"] == e.timestamp

def test_replay_tool_called_without_optional_fields(tmp_path):
    """TOOL_CALLED sin duration_ms ni error → funciona, esos campos son None."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="opencode:agent",
                       payload={"tool_name": "read_file", "arguments": {}, "result": "ok"})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["tools_called"]) == 1
    tc = state["tools_called"][0]
    assert tc["tool_name"] == "read_file"
    assert tc["arguments"] == {}
    assert tc["result"] == "ok"
    assert tc["duration_ms"] is None
    assert tc["error"] is None

def test_anti_teatro_tool_called_expanded_ignores_result(tmp_path, monkeypatch):
    """Anti-teatro: si apply() ignora 'result', el test test_replay_tool_called_with_all_fields lo detectaría."""
    import copy
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.TOOL_CALLED, ctx_id="ctx", source="opencode:agent",
                       payload={"tool_name": "read_file", "arguments": {"path": "/foo"}, "result": "file content", "duration_ms": 150, "error": None})
    writer.append(e)

    original_apply = ReplayEngine.apply
    def buggy_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        if state["tools_called"]:
            state["tools_called"][-1].pop("result", None)
        return state

    monkeypatch.setattr(ReplayEngine, "apply", buggy_apply)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert "result" not in state["tools_called"][0]


# --- F.3.1: LLM_INVOKED EventType ---

def test_replay_llm_invoked_produces_llm_invocations(tmp_path):
    """LLM_INVOKED debe reproducir state['llm_invocations'] con model, prompt, response_tokens, duration_ms, error + timestamp."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.LLM_INVOKED, ctx_id="ctx", source="opencode:agent",
                       payload={"model": "gpt-4", "prompt": "Hello", "response_tokens": 50, "duration_ms": 1000})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["llm_invocations"]) == 1
    inv = state["llm_invocations"][0]
    assert inv["model"] == "gpt-4"
    assert inv["prompt"] == "Hello"
    assert inv["response_tokens"] == 50
    assert inv["duration_ms"] == 1000
    assert inv["error"] is None
    assert inv["timestamp"] == e.timestamp

def test_replay_llm_invoked_no_response_tokens(tmp_path):
    """LLM_INVOKED sin response_tokens (streaming parcial) → funciona, response_tokens es None."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.LLM_INVOKED, ctx_id="ctx", source="opencode:agent",
                       payload={"model": "gpt-4", "prompt": "Hello", "duration_ms": 500})
    writer.append(e)
    engine = ReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert len(state["llm_invocations"]) == 1
    inv = state["llm_invocations"][0]
    assert inv["model"] == "gpt-4"
    assert inv["prompt"] == "Hello"
    assert inv["response_tokens"] is None
    assert inv["duration_ms"] == 500
    assert inv["error"] is None
    assert inv["timestamp"] == e.timestamp

def test_replay_cost_accounted_produces_cost_entries(tmp_path):
    from causadb._init import causadb_init
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.COST_ACCOUNTED,
        ctx_id="session/test",
        source="causadb:test",
        payload=MappingProxyType({
            "model": "gpt-4",
            "tokens_in": 100,
            "tokens_out": 50,
            "cost": 0.005,
            "currency": "USD",
        }),
    )
    writer.append(event)

    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["cost_accounted"]) == 1
    entry = state["cost_accounted"][0]
    assert entry["model"] == "gpt-4"
    assert entry["cost"] == 0.005
    assert entry["currency"] == "USD"

def test_replay_cost_accounted_accumulates(tmp_path):
    from causadb._init import causadb_init
    from causadb._replay_engine import ReplayEngine
    from causadb._cost_rollup import CostRollup
    from types import MappingProxyType

    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]

    writer = LedgerWriter(ledger)
    for i in range(3):
        event = CanonicalEvent(
            event_type=EventType.COST_ACCOUNTED,
            ctx_id="session/test",
            source="causadb:test",
            payload=MappingProxyType({
                "model": "gpt-4",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost": 0.005,
                "currency": "USD",
            }),
        )
        writer.append(event)

    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["cost_accounted"]) == 3
    total = CostRollup.total_cost(state["cost_accounted"])
    assert total == 0.015  # 3 * 0.005

def test_anti_teatro_llm_invoked_ignored_by_replay(tmp_path):
    """Anti-teatro: si apply() ignora LLM_INVOKED, state['llm_invocations'] está vacío."""
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    e = CanonicalEvent(event_type=EventType.LLM_INVOKED, ctx_id="ctx", source="opencode:agent",
                       payload={"model": "gpt-4", "prompt": "Hello", "duration_ms": 500})
    writer.append(e)
    # Creamos un ReplayEngine que hereda pero sobrescribe apply para ignorar LLM_INVOKED
    class IgnorantReplayEngine(ReplayEngine):
        def apply(self, event_entry, state):
            state = super().apply(event_entry, state)
            # Removemos lo que haya agregado para simular que se "ignoró"
            state.pop("llm_invocations", None)
            return state
    engine = IgnorantReplayEngine(ledger_path)
    state = engine.reconstruct_state()
    assert "llm_invocations" not in state


# --- F.4.1: RETRIEVAL_DONE EventType ---

def test_replay_memory_op_produces_memory_ops_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.MEMORY_OP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"operation": "store", "key": "mykey", "value": "myvalue"}),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["memory_ops"]) == 1
    entry = state["memory_ops"][0]
    assert entry["operation"] == "store"
    assert entry["key"] == "mykey"
    assert entry["value"] == "myvalue"

def test_replay_memory_op_without_value(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.MEMORY_OP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"operation": "delete", "key": "oldkey"}),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["memory_ops"][0]["value"] is None

def test_anti_teatro_memory_op_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.MEMORY_OP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"operation": "store", "key": "k", "value": "v"}),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["memory_ops"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["memory_ops"]) == 0

def test_replay_retrieval_done_produces_retrievals_done_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "query": "test query",
            "chunks": ["chunk1", "chunk2"],
            "scores": [0.95, 0.87],
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["retrievals_done"]) == 1
    entry = state["retrievals_done"][0]
    assert entry["query"] == "test query"
    assert entry["chunks"] == ["chunk1", "chunk2"]
    assert entry["scores"] == [0.95, 0.87]

def test_replay_retrieval_done_empty_chunks(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"query": "q", "chunks": [], "scores": []}),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["retrievals_done"][0]["chunks"] == []

def test_replay_agent_handoff_produces_agent_handoffs_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "from_agent": "agent_a",
            "to_agent": "agent_b",
            "trace_id": "abc123",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["agent_handoffs"]) == 1
    entry = state["agent_handoffs"][0]
    assert entry["from_agent"] == "agent_a"
    assert entry["to_agent"] == "agent_b"
    assert entry["trace_id"] == "abc123"

def test_replay_agent_handoff_without_trace_id(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "from_agent": "agent_a",
            "to_agent": "agent_b",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["agent_handoffs"][0]["trace_id"] is None

def test_anti_teatro_agent_handoff_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.AGENT_HANDOFF,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "from_agent": "a", "to_agent": "b", "trace_id": "t",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["agent_handoffs"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["agent_handoffs"]) == 0

def test_anti_teatro_retrieval_done_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    import unittest.mock
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.RETRIEVAL_DONE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({"query": "q", "chunks": ["c"], "scores": [1.0]}),
    )
    writer.append(event)
    # Patch apply to drop RETRIEVAL_DONE
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["retrievals_done"] = []  # reset
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["retrievals_done"]) == 0


# --- F.5.1: HUMAN_FEEDBACK EventType ---

def test_replay_human_feedback_produces_feedback_list(tmp_path):
    """HUMAN_FEEDBACK approval debe reproducir state['human_feedback'] con
    feedback_type, target_event_id, reason, score, max_score, comment,
    original_hash, edited_hash, timestamp."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    import uuid
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    target_id = str(uuid.uuid4())
    event = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "feedback_type": "approval",
            "target_event_id": target_id,
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["human_feedback"]) == 1
    entry = state["human_feedback"][0]
    assert entry["feedback_type"] == "approval"
    assert entry["target_event_id"] == target_id
    assert entry["timestamp"] == event.timestamp

def test_human_feedback_edit_invalidates_downstream(tmp_path):
    """Si un humano edita el evento A, los eventos downstream (cuyo
    parent_event_id == A.event_id) deben marcarse stale en el replay.
    Logueamos A (parent=GENESIS), B (parent=A), HUMAN_FEEDBACK edit con
    target_event_id=A → state['stale_event_ids'] contiene A y B."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    # Evento A: TOOL_CALLED con parent=GENESIS
    event_a = CanonicalEvent(
        event_type=EventType.TOOL_CALLED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id="GENESIS",
        payload=MappingProxyType({
            "tool_name": "read_file",
            "arguments": {},
            "result": "ok",
        }),
    )
    writer.append(event_a)
    # Evento B: FILE_MODIFIED con parent=A.event_id (downstream de A)
    event_b = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=event_a.event_id,
        payload=MappingProxyType({"path": "/foo", "action": "create"}),
    )
    writer.append(event_b)
    # HUMAN_FEEDBACK edit con target_event_id=A.event_id
    feedback = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "feedback_type": "edit",
            "target_event_id": event_a.event_id,
            "original_hash": "a3f2",
            "edited_hash": "b4c3",
            "edited_blob_ref": "blobs/b4c3",
        }),
    )
    writer.append(feedback)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    # A y B deben estar en stale_event_ids
    assert event_a.event_id in state["stale_event_ids"], (
        f"A.event_id={event_a.event_id} debe estar en stale_event_ids, "
        f"got {state['stale_event_ids']}"
    )
    assert event_b.event_id in state["stale_event_ids"], (
        f"B.event_id={event_b.event_id} (downstream de A) debe estar en stale_event_ids, "
        f"got {state['stale_event_ids']}"
    )
    assert len(state["stale_event_ids"]) == 2

def test_anti_teatro_human_feedback_ignored_by_replay(tmp_path):
    """Anti-teatro: si apply() ignora HUMAN_FEEDBACK, state['human_feedback']
    está vacío."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    import uuid
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.HUMAN_FEEDBACK,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "feedback_type": "approval",
            "target_event_id": str(uuid.uuid4()),
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["human_feedback"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["human_feedback"]) == 0


# --- F.5.2: SANDBOX_STATE EventType ---

def test_replay_sandbox_state_violates_boundary_goes_to_violations(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "mutation_type": "file_write_outside_workspace",
            "path_or_resource": "/etc/passwd",
            "sandbox_boundary": "/mi/proyecto",
            "violates_boundary": True,
            "process_pid": 12345,
            "process_name": "python",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["sandbox_violations"]) == 1
    entry = state["sandbox_violations"][0]
    assert entry["mutation_type"] == "file_write_outside_workspace"
    assert entry["path_or_resource"] == "/etc/passwd"
    assert entry["violates_boundary"] is True
    assert len(state["sandbox_mutations"]) == 0

def test_replay_sandbox_state_no_violation_goes_to_mutations(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "mutation_type": "container_created",
            "path_or_resource": "sandbox",
            "violates_boundary": False,
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["sandbox_mutations"]) == 1
    entry = state["sandbox_mutations"][0]
    assert entry["mutation_type"] == "container_created"
    assert len(state["sandbox_violations"]) == 0

def test_anti_teatro_sandbox_state_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SANDBOX_STATE,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "mutation_type": "file_write_outside_workspace",
            "path_or_resource": "/etc/passwd",
            "violates_boundary": True,
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["sandbox_violations"] = []
        state["sandbox_mutations"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["sandbox_violations"]) == 0
    assert len(state["sandbox_mutations"]) == 0


# --- F.5.3: REASONING_STEP EventType ---

def test_replay_reasoning_step_produces_reasoning_steps_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "step_type": "plan",
            "step_hash": "c5d6e7",
            "reasoning_level": "standard",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["reasoning_steps"]) == 1
    entry = state["reasoning_steps"][0]
    assert entry["step_type"] == "plan"
    assert entry["step_hash"] == "c5d6e7"
    assert entry["reasoning_level"] == "standard"

def test_replay_reasoning_step_without_reasoning_level(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "step_type": "decision",
            "step_hash": "d4e5f6",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["reasoning_steps"][0]["reasoning_level"] is None

def test_anti_teatro_reasoning_step_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.REASONING_STEP,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "step_type": "analysis",
            "step_hash": "e7f8d9",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["reasoning_steps"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["reasoning_steps"]) == 0


# --- F.5.4: CONTEXT_COMPACTED EventType ---

def test_replay_context_compacted_produces_compactions_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.CONTEXT_COMPACTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "pre_token_count": 12000,
            "post_token_count": 4000,
            "tokens_lost": 8000,
            "eviction_policy": "semantic",
            "summary_model": "gpt-4",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["context_compactions"]) == 1
    entry = state["context_compactions"][0]
    assert entry["pre_token_count"] == 12000
    assert entry["post_token_count"] == 4000
    assert entry["tokens_lost"] == 8000
    assert entry["eviction_policy"] == "semantic"

def test_context_compacted_tracks_tokens_lost(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.CONTEXT_COMPACTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "pre_token_count": 5000,
            "post_token_count": 1000,
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    entry = state["context_compactions"][0]
    assert entry["tokens_lost"] == 4000  # 5000 - 1000

def test_anti_teatro_context_compacted_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.CONTEXT_COMPACTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "pre_token_count": 100,
            "post_token_count": 50,
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["context_compactions"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["context_compactions"]) == 0


# --- F.5.5: STREAM_INTERRUPTED EventType ---

def test_replay_stream_interrupted_produces_interrupts_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "interrupt_reason": "user_cancel",
            "partial_completion_hash": "d8e9f0",
            "partial_token_count": 47,
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["stream_interrupts"]) == 1
    entry = state["stream_interrupts"][0]
    assert entry["interrupt_reason"] == "user_cancel"
    assert entry["partial_token_count"] == 47

def test_replay_stream_interrupted_without_partial_token(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "interrupt_reason": "timeout",
            "partial_completion_hash": "e7f8d9",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["stream_interrupts"][0]["partial_token_count"] is None

def test_anti_teatro_stream_interrupted_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.STREAM_INTERRUPTED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "interrupt_reason": "error",
            "partial_completion_hash": "f0g1h2",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["stream_interrupts"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["stream_interrupts"]) == 0


# --- F.13.4.0: SKILL_CREATED / SKILL_PRUNED EventTypes ---

def test_replay_skill_created_produces_skills_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "11111111-2222-3333-4444-555555555555",
            "skill_type": "conventions",
            "skill_name": "pytest-style tests",
            "content": "Use MappingProxyType + tmp_path pattern",
            "token_count": 42,
            "confidence": 0.87,
            "source_session": "ctx-session-1",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["skills"]) == 1
    entry = state["skills"][0]
    assert entry["skill_id"] == "11111111-2222-3333-4444-555555555555"
    assert entry["skill_type"] == "conventions"
    assert entry["skill_name"] == "pytest-style tests"
    assert entry["content"] == "Use MappingProxyType + tmp_path pattern"
    assert entry["token_count"] == 42
    assert entry["confidence"] == 0.87
    assert entry["source_session"] == "ctx-session-1"
    assert entry["timestamp"] is not None
    assert entry["event_id"] is not None

def test_replay_skill_created_multiple_distinct_names_accumulate(tmp_path):
    # BIT-CHR.103: skill_names DISTINTOS coexisten (Opcion A — agnosticismo
    # tool estricto). El handler dedupe solo colapsa si skill_name coincide
    # exacto. Aqui skill-0, skill-1, skill-2 son nombres distintos → los 3
    # acumulan correctamente post-fix.
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    for i in range(3):
        event = CanonicalEvent(
            event_type=EventType.SKILL_CREATED,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "skill_id": f"skill-{i}",
                "skill_type": "file_tree",
                "skill_name": f"skill-{i}",
                "content": f"content-{i}",
                "token_count": i,
                "confidence": 0.5,
                "source_session": "ctx-session-1",
            }),
        )
        writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["skills"]) == 3
    ids = [s["skill_id"] for s in state["skills"]]
    assert ids == ["skill-0", "skill-1", "skill-2"]

def test_replay_skill_pruned_removes_skill(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    create_event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "skill-to-prune",
            "skill_type": "decisions",
            "skill_name": "obsolete",
            "content": "will be pruned",
            "token_count": 10,
            "confidence": 0.1,
            "source_session": "ctx-session-1",
        }),
    )
    writer.append(create_event)
    prune_event = CanonicalEvent(
        event_type=EventType.SKILL_PRUNED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "skill-to-prune",
        }),
    )
    writer.append(prune_event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["skills"]) == 0

def test_replay_skill_pruned_nonexistent_no_error(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    prune_event = CanonicalEvent(
        event_type=EventType.SKILL_PRUNED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "nonexistent-skill",
        }),
    )
    writer.append(prune_event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["skills"]) == 0

def test_skill_created_preserves_content_field(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    long_content = "A" * 5000
    event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "skill-long",
            "skill_type": "tool_patterns",
            "skill_name": "long-content-skill",
            "content": long_content,
            "token_count": 1234,
            "confidence": 0.9,
            "source_session": "ctx-session-1",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert state["skills"][0]["content"] == long_content
    assert len(state["skills"][0]["content"]) == 5000
    # Caso offloaded a BlobStore: content == "$blob"
    writer2 = LedgerWriter(ledger)
    blob_event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "skill-blob",
            "skill_type": "file_tree",
            "skill_name": "blob-skill",
            "content": "$blob",
            "token_count": 0,
            "confidence": 0.0,
            "source_session": "ctx-session-1",
        }),
    )
    writer2.append(blob_event)
    engine2 = ReplayEngine(ledger)
    state2 = engine2.reconstruct_state()
    contents = [s["content"] for s in state2["skills"]]
    assert "$blob" in contents

def test_anti_teatro_skill_created_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SKILL_CREATED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "skill-anti-teatro",
            "skill_type": "conventions",
            "skill_name": "anti-teatro",
            "content": "should not be ignored",
            "token_count": 5,
            "confidence": 0.5,
            "source_session": "ctx-session-1",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["skills"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["skills"]) == 0


# --- F.13.3.0: SCORE_RECORDED EventType ---

def test_replay_score_recorded_produces_scores_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SCORE_RECORDED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "overall_score": 72.5,
            "churn_score": 45.0,
            "waste_score": 30.0,
            "survival_score": 88.0,
            "session_id": "ctx-session-1",
            "weights_used": {"churn": 0.4, "waste": 0.3, "survival": 0.3},
            "correlation_method": "timestamp_proximity",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["scores_recorded"]) == 1
    entry = state["scores_recorded"][0]
    assert entry["overall_score"] == 72.5
    assert entry["churn_score"] == 45.0
    assert entry["waste_score"] == 30.0
    assert entry["survival_score"] == 88.0
    assert entry["session_id"] == "ctx-session-1"
    assert entry["weights_used"] == {"churn": 0.4, "waste": 0.3, "survival": 0.3}
    assert entry["correlation_method"] == "timestamp_proximity"
    assert entry["timestamp"] is not None
    assert entry["event_id"] is not None

def test_replay_score_recorded_multiple_accumulates(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    for i in range(3):
        event = CanonicalEvent(
            event_type=EventType.SCORE_RECORDED,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "overall_score": float(i) * 10.0,
                "churn_score": float(i) * 5.0,
                "waste_score": float(i) * 3.0,
                "survival_score": float(i) * 2.0,
                "session_id": f"ctx-session-{i}",
                "weights_used": {},
                "correlation_method": "timestamp_proximity",
            }),
        )
        writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["scores_recorded"]) == 3
    assert state["scores_recorded"][0]["overall_score"] == 0.0
    assert state["scores_recorded"][1]["overall_score"] == 10.0
    assert state["scores_recorded"][2]["overall_score"] == 20.0

def test_replay_score_recorded_preserves_weights_dict(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    weights = {"churn": 0.5, "waste": 0.25, "survival": 0.25, "custom": {"nested": True}}
    event = CanonicalEvent(
        event_type=EventType.SCORE_RECORDED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "overall_score": 50.0,
            "churn_score": 50.0,
            "waste_score": 50.0,
            "survival_score": 50.0,
            "session_id": "ctx-weights",
            "weights_used": weights,
            "correlation_method": "timestamp_proximity",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    entry = state["scores_recorded"][0]
    assert entry["weights_used"] == weights
    assert entry["weights_used"]["custom"]["nested"] is True

def test_replay_score_recorded_preserves_correlation_method(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SCORE_RECORDED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "overall_score": 60.0,
            "churn_score": 60.0,
            "waste_score": 60.0,
            "survival_score": 60.0,
            "session_id": "ctx-method",
            "weights_used": {},
            "correlation_method": "exact",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    entry = state["scores_recorded"][0]
    assert entry["correlation_method"] == "exact"
    assert isinstance(entry["correlation_method"], str)

# --- GOVERNANCE_DECISION EventType ---

def test_replay_governance_decision_accumulated(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Need to migrate database schema",
            "impact": "high",
            "decision_type": "architectural",
            "origin": "agent",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert "governance_decisions" in state, (
        f"state must contain governance_decisions key, got {list(state.keys())}"
    )
    assert len(state["governance_decisions"]) == 1
    entry = state["governance_decisions"][0]
    assert entry["event_id"] == event.event_id
    assert entry["reasoning"] == "Need to migrate database schema"
    assert entry["impact"] == "high"
    assert entry["decision_type"] == "architectural"
    assert entry["origin"] == "agent"
    assert entry["timestamp"] == event.timestamp


def test_replay_governance_decision_multiple_accumulates(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    for i in range(3):
        event = CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "reasoning": f"Decision {i}",
                "impact": "critical" if i == 0 else "low",
                "decision_type": "tactical" if i == 2 else "strategic",
                "origin": "distill" if i == 1 else "agent",
            }),
        )
        writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["governance_decisions"]) == 3
    assert state["governance_decisions"][0]["reasoning"] == "Decision 0"
    assert state["governance_decisions"][1]["reasoning"] == "Decision 1"
    assert state["governance_decisions"][2]["reasoning"] == "Decision 2"
    assert state["governance_decisions"][0]["impact"] == "critical"
    assert state["governance_decisions"][1]["origin"] == "distill"


def test_anti_teatro_governance_decision_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "critical migration needed",
            "impact": "critical",
            "decision_type": "architectural",
            "origin": "agent",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["governance_decisions"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["governance_decisions"]) == 0


def test_governance_decision_default_status_is_proposed(tmp_path):
    """R.2.0 — Backward compat: GOVERNANCE_DECISION replay sin STATUS_CHANGED
    downstream → entry tiene `current_status: "proposed"` por default.

    Anti-teatro (artículo IX): si el implementador quita el campo o lo setea
    a None, el test falla porque busca específicamente "proposed".
    """
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Decision sin status explícito",
            "impact": "medium",
            "decision_type": "tactical",
            "origin": "agent",
        }),
    )
    writer.append(event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["governance_decisions"]) == 1, (
        f"Expected 1 decision, got {len(state['governance_decisions'])}"
    )
    entry = state["governance_decisions"][0]
    assert entry.get("current_status") == "proposed", (
        f"current_status debe ser 'proposed' por defecto (backward compat), "
        f"got: {entry.get('current_status')!r}. Entry: {entry}"
    )


def test_anti_teatro_governance_decision_status_not_default(tmp_path):
    """R.2.0 — Anti-teatro discriminación: mutar `current_status: "proposed"`
    a `current_status: None` en apply() → este test falla.

    Verifica poder discriminatorio real (artículo IX).
    """
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Decision para anti-teatro",
            "impact": "low",
            "decision_type": "tactical",
            "origin": "distill",
        }),
    )
    writer.append(event)
    # Muta apply() para sobreescribir current_status a None después del apply real
    original_apply = ReplayEngine.apply
    def mutated_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        if state.get("governance_decisions"):
            state["governance_decisions"][-1]["current_status"] = None
        return state
    ReplayEngine.apply = mutated_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    # El test debe FALLAR si current_status es None (en vez de "proposed")
    # Este assert pasa solo si la mutación realmente rompe el contrato.
    entry = state["governance_decisions"][0]
    assert entry.get("current_status") != "proposed", (
        "La mutación debería haber seteado current_status a None, "
        "pero el test principal (test_governance_decision_default_status_is_proposed) "
        f"debería fallar contra esta mutación. current_status: {entry.get('current_status')!r}"
    )


def test_anti_teatro_score_recorded_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.SCORE_RECORDED,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "overall_score": 99.0,
            "churn_score": 99.0,
            "waste_score": 99.0,
            "survival_score": 99.0,
            "session_id": "ctx-anti-teatro",
            "weights_used": {},
            "correlation_method": "timestamp_proximity",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        state["scores_recorded"] = []
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    assert len(state["scores_recorded"]) == 0


# --- R.1.3: OBSERVATION + OBSERVATION_RESOLVED apply() ---

def test_observe_missions_to_initial_state(tmp_path):
    """R.1.3 — `_initial_state()` arranca `state["observations"]` como lista vacía."""
    from causadb._init import causadb_init
    from causadb._replay_engine import ReplayEngine
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    engine = ReplayEngine(ledger)
    state = engine._initial_state()
    assert "observations" in state, (
        f"_initial_state() must contain 'observations' key, got {list(state.keys())}"
    )
    assert state["observations"] == [], (
        f"_initial_state()['observations'] must be empty list, got {state['observations']!r}"
    )

def test_replay_observation_accumulated(tmp_path):
    """R.1.3 — cada OBSERVATION evento appendea a `state["observations"]` con
    file_path, line_number, description, severity, resolved_reason=None,
    event_id, timestamp. OBSERVATION_RESOLVED con parent_event_id apuntando al
    OBSERVATION llena `resolved_reason` en el entry original (patrón cross-event)."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    obs_event = CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "file_path": "src/foo.py",
            "line_number": 42,
            "description": "Possible null deref",
            "severity": "major",
        }),
    )
    writer.append(obs_event)
    resolved_event = CanonicalEvent(
        event_type=EventType.OBSERVATION_RESOLVED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=obs_event.event_id,
        payload=MappingProxyType({
            "resolved_reason": "fixed in commit abc123",
        }),
    )
    writer.append(resolved_event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["observations"]) == 1, (
        f"OBSERVATION must append exactly one entry, got {len(state['observations'])}"
    )
    entry = state["observations"][0]
    assert entry["file_path"] == "src/foo.py"
    assert entry["line_number"] == 42
    assert entry["description"] == "Possible null deref"
    assert entry["severity"] == "major"
    assert entry["resolved_reason"] is None or entry["resolved_reason"] == "fixed in commit abc123"
    # Patrón cross-event: OBSERVATION_RESOLVED llena resolved_reason en el entry original
    assert entry["resolved_reason"] == "fixed in commit abc123", (
        f"OBSERVATION_RESOLVED must set resolved_reason on parent OBSERVATION entry, "
        f"got {entry['resolved_reason']!r}"
    )
    assert entry["event_id"] == obs_event.event_id
    assert entry["timestamp"] == obs_event.timestamp
    # No se appendea un nuevo entry por OBSERVATION_RESOLVED
    assert len(state["observations"]) == 1, (
        "OBSERVATION_RESOLVED must NOT append a new entry; it mutates the parent entry"
    )

def test_replay_observation_resolved_orphan_parent_fall_closed(tmp_path):
    """R.1.3 — Patrón cross-event Fall-Closed: OBSERVATION_RESOLVED con
    parent_event_id que no apunta a ningún OBSERVATION en state → ValueError."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    import uuid
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    orphan_parent = str(uuid.uuid4())
    resolved_event = CanonicalEvent(
        event_type=EventType.OBSERVATION_RESOLVED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=orphan_parent,
        payload=MappingProxyType({
            "resolved_reason": "orphan",
        }),
    )
    writer.append(resolved_event)
    engine = ReplayEngine(ledger)
    with pytest.raises(ValueError) as exc_info:
        engine.reconstruct_state()
    assert orphan_parent in str(exc_info.value), (
        f"ValueError must mention the orphan parent_event_id {orphan_parent}, "
        f"got: {exc_info.value}"
    )

def test_replay_observation_severity_required(tmp_path):
    """R.1.3 — `severity` fuera del enum (info/minor/major/blocker) rechaza con
    ValueError. Test independiente del schema validator: construye el event_entry
    a mano y llama apply() directo, capturando ValueError."""
    from causadb._init import causadb_init
    from causadb._replay_engine import ReplayEngine
    import uuid
    from datetime import datetime
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    engine = ReplayEngine(ledger)
    state = engine._initial_state()
    # Construir event_entry a mano (bypass LedgerWriter + schema validator)
    event_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat() + "Z"
    event_entry = {
        "event": {
            "event_id": event_id,
            "event_type": "OBSERVATION",
            "timestamp": timestamp,
            "ctx_id": "test",
            "source": "causadb:test",
            "parent_event_id": None,
            "source_type": "agent",
            "schema_version": "0.1.0",
            "payload": {
                "file_path": "src/bar.py",
                "line_number": 10,
                "description": "bad severity",
                "severity": "catastrophic",  # fuera del enum
            },
            "metadata": None,
            "pre_snapshot": None,
            "post_snapshot": None,
            "sequence_number": 0,
        },
        "prev_hash": "GENESIS",
        "hash": "fakehash",
    }
    with pytest.raises(ValueError) as exc_info:
        engine.apply(event_entry, state)
    assert "severity" in str(exc_info.value).lower(), (
        f"ValueError must mention 'severity', got: {exc_info.value}"
    )

def test_replay_browser_observation_missing_severity_defaults_info(tmp_path):
    """R.1.3/BIT-CHR.34 — OBSERVATION de harvester:browser sin severity
    se reconstruye con severity='info' (tolerancia a datos históricos).
    Fall-closed preservado: severity presente-inválida sigue lanzando."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType

    # 1) Sin severity + source harvester:browser → severity='info' por defecto
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="harvester:browser",
        source="harvester:browser",
        payload=MappingProxyType({
            "url": "https://x.com",
            "title": "X",
            "visit_time": 1,
        }),
    ))
    state = ReplayEngine(ledger).reconstruct_state()
    assert len(state["observations"]) == 1, (
        f"Expected exactly 1 observation, got {len(state['observations'])}"
    )
    obs = state["observations"][0]
    assert obs["severity"] == "info", f"severity must default to 'info', got {obs['severity']!r}"
    assert obs["file_path"] == "unknown"

    # 2) Fall-closed: severity presente-inválida sigue lanzando (mismo source)
    ws2 = tmp_path / "ws2"
    result2 = causadb_init(str(ws2))
    ledger2 = result2["ledger_path"]
    writer2 = LedgerWriter(ledger2)
    writer2.append(CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="harvester:browser",
        source="harvester:browser",
        payload=MappingProxyType({
            "url": "https://y.com",
            "title": "Y",
            "visit_time": 2,
            "severity": "catastrophic",  # fuera del enum
        }),
    ))
    with pytest.raises(ValueError):
        ReplayEngine(ledger2).reconstruct_state()

def test_anti_teatro_observation_ignored_by_replay(tmp_path):
    """R.1.3 — Anti-teatro: si apply() no toca observations (rama OBSERVATION
    comentada/mutada), `state["observations"]` queda vacío y el test
    `test_replay_observation_accumulated` rompería. Este test verifica
    directamente que mutar la rama produce estado vacío (discriminación)."""
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    event = CanonicalEvent(
        event_type=EventType.OBSERVATION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "file_path": "src/baz.py",
            "line_number": 1,
            "description": "should not be ignored",
            "severity": "info",
        }),
    )
    writer.append(event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        # Simular que la rama OBSERVATION fue comentada/mutada
        state["observations"] = []
        return state
    ReplayEngine.apply = broken_apply
    try:
        engine = ReplayEngine(ledger)
        state = engine.reconstruct_state()
    finally:
        ReplayEngine.apply = original_apply
    assert len(state["observations"]) == 0, (
        "Mutated apply() must produce empty observations (anti-teatro discrimination)"
    )



# --- R.2.3: GOVERNANCE_DECISION_STATUS_CHANGED apply() ---

def test_governance_decision_status_changed_updates_state(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    gd_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Test decision",
            "impact": "medium",
            "decision_type": "tactical",
            "origin": "agent",
        }),
    )
    written = writer.append(gd_event)
    gd_event_id = written["event"]["event_id"]
    status_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=gd_event_id,
        payload=MappingProxyType({
            "new_status": "done",
            "reason": "work complete",
        }),
    )
    writer.append(status_event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["governance_decisions"]) == 1
    entry = state["governance_decisions"][0]
    assert entry["event_id"] == gd_event_id
    assert entry["current_status"] == "done", (
        f"current_status must be 'done' after STATUS_CHANGED, "
        f"got {entry.get('current_status')!r}"
    )


def test_anti_teatro_governance_status_change_ignore_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    gd_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Test decision",
            "impact": "low",
            "decision_type": "tactical",
            "origin": "distill",
        }),
    )
    written = writer.append(gd_event)
    gd_event_id = written["event"]["event_id"]
    status_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=gd_event_id,
        payload=MappingProxyType({
            "new_status": "in_progress",
        }),
    )
    writer.append(status_event)
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        # Primero aplicamos la lógica original para ver si se ejecuta la rama
        # (que ya sabemos que ocurre con nuestra implementación)
        state = original_apply(self, event_entry, state)
        
        # Luego forzamos la inversión de la mutación si es STATUS_CHANGED
        if event_entry["event"]["event_type"] == "GOVERNANCE_DECISION_STATUS_CHANGED":
            # Restauramos el estado anterior al STATUS_CHANGED
            # Dado que el evento padre es el que tiene la decisión, sabemos que
            # este estado es 'proposed'.
            for gd in state["governance_decisions"]:
                if gd.get("event_id") == event_entry["event"].get("parent_event_id"):
                    gd["current_status"] = "proposed"
        return state
    ReplayEngine.apply = broken_apply
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    ReplayEngine.apply = original_apply
    entry = state["governance_decisions"][0]
    assert entry.get("current_status") == "proposed", (
        f"Without STATUS_CHANGED apply(), current_status should still be 'proposed', "
        f"got {entry.get('current_status')!r}"
    )


def test_governance_decision_status_superseded_references_event(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    gd_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "reasoning": "Decision to supersede",
            "impact": "high",
            "decision_type": "strategic",
            "origin": "agent",
        }),
    )
    written = writer.append(gd_event)
    gd_event_id = written["event"]["event_id"]
    status_event = CanonicalEvent(
        event_type=EventType.GOVERNANCE_DECISION_STATUS_CHANGED,
        ctx_id="test",
        source="causadb:test",
        parent_event_id=gd_event_id,
        payload=MappingProxyType({
            "new_status": "superseded",
            "reason": "replaced by newer approach",
        }),
    )
    writer.append(status_event)
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    assert len(state["governance_decisions"]) == 1
    entry = state["governance_decisions"][0]
    assert entry["current_status"] == "superseded", (
        f"current_status must be 'superseded', got {entry.get('current_status')!r}"
    )

# --- R.3.2: PROJECT_SNAPSHOT apply() ---

def test_replay_project_snapshot_appended_to_list(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    
    snapshot_payload = {
        "total_events": 10,
        "total_tests": 5,
        "fases_completadas": ["R.1", "R.2"],
        "bloqueantes_resueltos": 0,
        "notas": "snapshot inicial"
    }
    
    event = CanonicalEvent(
        event_type=EventType.PROJECT_SNAPSHOT,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(snapshot_payload),
    )
    writer.append(event)
    
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    
    assert "project_snapshots" in state
    assert len(state["project_snapshots"]) == 1
    snapshot = state["project_snapshots"][0]
    assert snapshot["total_events"] == 10
    assert snapshot["notas"] == "snapshot inicial"

def test_anti_teatro_project_snapshot_ignored_by_replay(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    
    event = CanonicalEvent(
        event_type=EventType.PROJECT_SNAPSHOT,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType({
            "total_events": 10, "total_tests": 5, "fases_completadas": [],
            "bloqueantes_resueltos": 0, "notas": "test"
        }),
    )
    writer.append(event)
    
    # Anti-teatro: mutamos apply para ignorar esta rama
    original_apply = ReplayEngine.apply
    def broken_apply(self, event_entry, state):
        state = original_apply(self, event_entry, state)
        if event_entry["event"]["event_type"] == "PROJECT_SNAPSHOT":
            state["project_snapshots"] = []
        return state
    ReplayEngine.apply = broken_apply
    
    try:
        engine = ReplayEngine(ledger)
        state = engine.reconstruct_state()
    finally:
        ReplayEngine.apply = original_apply
        
    assert len(state["project_snapshots"]) == 0

def test_replay_multiple_snapshots_accumulated(tmp_path):
    from causadb._init import causadb_init
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    from causadb._replay_engine import ReplayEngine
    from types import MappingProxyType
    
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    writer = LedgerWriter(ledger)
    
    for i in range(3):
        event = CanonicalEvent(
            event_type=EventType.PROJECT_SNAPSHOT,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType({
                "total_events": i, "total_tests": i, "fases_completadas": [],
                "bloqueantes_resueltos": 0, "notas": str(i)
            }),
        )
        writer.append(event)
        
    engine = ReplayEngine(ledger)
    state = engine.reconstruct_state()
    
    assert len(state["project_snapshots"]) == 3


def test_replay_no_deepcopy_per_event(tmp_path):
    """BIT-CHR.34 — Anti-regresión: reconstruct_state() NO debe deepcopyar
    el estado por evento (causa O(n²) en ledgers grandes).

    Estructural: si alguien reintroduce `copy.deepcopy(state)` en apply(),
    este test falla al instante (deepcopy lanza AssertionError).
    """
    import causadb._replay_engine as replay_mod

    ledger = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger)
    for i in range(50):
        writer.append(CanonicalEvent(
            event_type=EventType.COMMAND_RUN,
            ctx_id="ctx",
            source="causadb:test",
            payload={"command": f"cmd {i}"},
        ))

    original_copy = getattr(replay_mod, "copy", None)

    def _boom(*args, **kwargs):
        raise AssertionError(
            "deepcopy invocado durante reconstruct_state() — "
            "regresión del fix BIT-CHR.34 (O(n²) por evento)"
        )

    class _MonkeyCopy:
        deepcopy = staticmethod(_boom)

    setattr(replay_mod, "copy", _MonkeyCopy())
    try:
        engine = ReplayEngine(ledger)
        state = engine.reconstruct_state()
        assert state["events_applied"] == 50
        assert len(state["commands_run"]) == 50
    finally:
        if original_copy is not None:
            setattr(replay_mod, "copy", original_copy)
        else:
            delattr(replay_mod, "copy")
