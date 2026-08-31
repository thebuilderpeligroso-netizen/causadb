"""Tests for skill dedupe by skill_name (Fix BIT-CHR.103). Anti-teatro Art. IX."""
import json
import time
from types import MappingProxyType
from causadb._init import causadb_init
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._skill_registry import register_skill, load_skills, distill_post_harvest
from causadb._replay_engine import ReplayEngine


def _make_ledger(tmp_path):
    ws = tmp_path / "ws"
    return causadb_init(str(ws))["ledger_path"]


def _sample_skill(skill_type="file_tree", name="test", confidence=0.8, tokens=100, skill_id=None, content="content"):
    d = {
        "skill_type": skill_type, "skill_name": name, "content": content,
        "token_count": tokens, "confidence": confidence, "source_session": "test",
    }
    if skill_id:
        d["skill_id"] = skill_id
    return d


def test_register_skill_replaces_existing_same_name(tmp_path):
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="repeated_tools", content="v1"))
    register_skill(ledger, _sample_skill(name="repeated_tools", content="v2"))
    skills = load_skills(ledger)
    matching = [s for s in skills if s["skill_name"] == "repeated_tools"]
    assert len(matching) == 1, f"expected 1, got {len(matching)}"
    assert matching[0]["content"] == "v2", f"last wins, got {matching[0]['content']}"


def test_register_skill_keeps_both_if_different_name(tmp_path):
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="tree_a", content="a"))
    register_skill(ledger, _sample_skill(name="tree_b", content="b"))
    skills = load_skills(ledger)
    names = sorted(s["skill_name"] for s in skills)
    assert names == ["tree_a", "tree_b"], f"coexistencia Opcion A, got {names}"


def test_replay_collapses_duplicates_by_name(tmp_path):
    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)
    for i in range(5):
        writer.append(CanonicalEvent(
            event_type=EventType.SKILL_CREATED, ctx_id="test", source="causadb:test",
            payload=MappingProxyType({
                "skill_id": f"id-{i}", "skill_type": "file_tree", "skill_name": "same_name",
                "content": f"v{i}", "token_count": 10, "confidence": 0.5, "source_session": "s",
            }),
        ))
    state = ReplayEngine(ledger).reconstruct_state()
    same = [s for s in state["skills"] if s["skill_name"] == "same_name"]
    assert len(same) == 1, f"expected 1, got {len(same)}"
    assert same[0]["content"] == "v4", f"expected v4 (last), got {same[0]['content']}"


def test_replay_skill_pruned_is_noop_if_already_replaced(tmp_path):
    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(
        event_type=EventType.SKILL_CREATED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "A", "skill_type": "file_tree", "skill_name": "X",
            "content": "old", "token_count": 10, "confidence": 0.5, "source_session": "s",
        }),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.SKILL_CREATED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({
            "skill_id": "B", "skill_type": "file_tree", "skill_name": "X",
            "content": "new", "token_count": 10, "confidence": 0.9, "source_session": "s",
        }),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.SKILL_PRUNED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"skill_id": "A"}),
    ))
    state = ReplayEngine(ledger).reconstruct_state()
    same = [s for s in state["skills"] if s["skill_name"] == "X"]
    assert len(same) == 1
    assert same[0]["skill_id"] == "B"
    assert same[0]["content"] == "new"


def test_load_skills_returns_desc_order_by_default(tmp_path):
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(name="s1", content="old"))
    register_skill(ledger, _sample_skill(name="s2", content="new"))
    skills = load_skills(ledger)
    assert skills[0]["skill_name"] == "s2"
    assert skills[1]["skill_name"] == "s1"


def test_load_skills_limit_3_returns_3(tmp_path):
    ledger = _make_ledger(tmp_path)
    for i in range(5):
        register_skill(ledger, _sample_skill(name=f"sk{i}"))
    skills = load_skills(ledger, limit=3)
    assert len(skills) == 3


def test_causadb_skill_list_propagates_limit(tmp_path):
    from causadb.mcp._tools import causadb_skill_list
    ledger = _make_ledger(tmp_path)
    for i in range(5):
        register_skill(ledger, _sample_skill(name=f"sk{i}"))
    result = json.loads(causadb_skill_list(ledger, limit=2))
    assert result["count"] == 2
    assert len(result["skills"]) == 2
    assert result.get("limit") == 2


def test_distill_post_harvest_does_not_prune_other_skill_names(tmp_path):
    ledger = _make_ledger(tmp_path)
    register_skill(ledger, _sample_skill(skill_type="file_tree", name="old_tree", content="manual"))
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "new_file.py", "action": "modify"}),
    ))
    result = distill_post_harvest(ledger, source_type="hermes")
    assert result["status"] == "ok"
    skills = load_skills(ledger)
    file_trees = [s for s in skills if s["skill_type"] == "file_tree"]
    names = [s["skill_name"] for s in file_trees]
    assert "old_tree" in names, f"old_tree coexist (Opcion A), got {names}"


def test_replay_collapses_540_duplicates_by_name(tmp_path):
    ledger = _make_ledger(tmp_path)
    writer = LedgerWriter(ledger)
    for i in range(540):
        writer.append(CanonicalEvent(
            event_type=EventType.SKILL_CREATED, ctx_id="test", source="causadb:test",
            payload=MappingProxyType({
                "skill_id": f"id-{i}", "skill_type": "tool_patterns", "skill_name": "repeated_tools",
                "content": f"v{i}", "token_count": 10, "confidence": 0.5, "source_session": "s",
            }),
        ))
    start = time.monotonic()
    state = ReplayEngine(ledger).reconstruct_state()
    replay_elapsed = time.monotonic() - start
    same = [s for s in state["skills"] if s["skill_name"] == "repeated_tools"]
    assert len(same) == 1, f"expected 1, got {len(same)}"
    assert same[0]["content"] == "v539"
    assert replay_elapsed < 1.0, f"replay too slow: {replay_elapsed:.2f}s"
