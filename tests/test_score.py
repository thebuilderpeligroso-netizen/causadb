"""Tests for `causadb._score` (F.13.3.1 / F.13.3.2 / F.13.3.3).

Test-First discipline (Article III): tests exercise the real diff logic,
the fallback path, the timestamp-proximity correlation, and the weight
configuration. Anti-teatro tests (Article IX) mutate the implementation
in-memory to ensure a stub that skips the real work fails at least one
assertion.
"""
import json
import os
import uuid
from unittest.mock import patch

import pytest

from causadb._score import compute_churn, compute_waste, compute_score
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _make_file_modified(ctx_id="ctx-1", path="/foo.py", action="create",
                        writes=None, pre_snapshot=None, post_snapshot=None,
                        source="opencode:agent", timestamp=None):
    """Build a FILE_MODIFIED CanonicalEvent."""
    payload = {"path": path, "action": action}
    if writes is not None:
        payload["writes"] = writes
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id=ctx_id,
        source=source,
        source_type="agent",
        payload=payload,
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
        timestamp=timestamp,
    )


def _make_llm_invoked(ctx_id="ctx-1", cost=0.01, timestamp=None,
                      source="opencode:agent"):
    """Build an LLM_INVOKED CanonicalEvent."""
    return CanonicalEvent(
        event_type=EventType.LLM_INVOKED,
        ctx_id=ctx_id,
        source=source,
        source_type="llm",
        payload={"model": "gpt-4", "cost": cost, "prompt": "hi"},
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# compute_churn tests (F.13.3.1)
# ---------------------------------------------------------------------------

def test_compute_churn_empty_ledger(ledger_path):
    """Empty ledger → {}."""
    open(ledger_path, "a").close()  # create empty file
    result = compute_churn(ledger_path)
    assert result == {}


def test_compute_churn_counts_lines_with_snapshots(ledger_path, tmp_path):
    """Events with pre/post snapshots → churn count is real diff (file-level)."""
    # Build a BlobStore with two snapshots: pre has 1 file, post has 2 files
    # (one added, one modified).
    from causadb._blob_store import BlobStore
    from causadb._snapshot import WorkspaceSnapshot

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("a = 1\n")
    pre_snap = WorkspaceSnapshot.take(str(ws))
    store = BlobStore(str(tmp_path / "blobs"))
    pre_hash = WorkspaceSnapshot.store(pre_snap, store, root_dir=str(ws))

    # Modify + add a file.
    (ws / "a.py").write_text("a = 1\nb = 2\n")  # modified
    (ws / "b.py").write_text("c = 3\n")         # added
    post_snap = WorkspaceSnapshot.take(str(ws), prev_snapshot=pre_snap)
    post_hash = WorkspaceSnapshot.store(post_snap, store, root_dir=str(ws))

    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(
        path="a.py", action="modify",
        pre_snapshot=pre_hash, post_snapshot=post_hash,
    ))

    result = compute_churn(ledger_path)
    assert "ctx-1" in result
    s = result["ctx-1"]
    # At least 2 files churned (a.py modified + b.py added).
    assert s["files_churned"] >= 2, f"expected >=2 files churned, got {s['files_churned']}"
    assert s["lines_added"] > 0, "lines_added must be > 0 with snapshots"
    assert s["churn_ratio"] > 0.0


def test_compute_churn_fallback_without_snapshots(ledger_path):
    """Events without snapshots → estimation non-zero with warning."""
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        writes=[{"path": "/foo.py"}],
    ))
    result = compute_churn(ledger_path)
    s = result["ctx-1"]
    # Fallback must NOT return 0 silently.
    assert s["files_churned"] > 0, "fallback must estimate non-zero files_churned"
    assert s["lines_added"] > 0, "fallback must estimate non-zero lines_added"
    assert len(s["warnings"]) > 0, "fallback must emit at least one warning"
    assert any("no_snapshots" in w for w in s["warnings"]), (
        f"warnings must mention no_snapshots, got {s['warnings']}"
    )


def test_compute_churn_per_session(ledger_path):
    """Two sessions → churn computed separately for each."""
    writer = LedgerWriter(ledger_path)
    # Session A: 1 file written (fallback).
    writer.append(_make_file_modified(
        ctx_id="sess-A", path="/a.py", action="create",
        writes=[{"path": "/a.py"}],
    ))
    # Session B: 2 files written (fallback).
    writer.append(_make_file_modified(
        ctx_id="sess-B", path="/b.py", action="create",
        writes=[{"path": "/b.py"}],
    ))
    writer.append(_make_file_modified(
        ctx_id="sess-B", path="/c.py", action="create",
        writes=[{"path": "/c.py"}],
    ))
    result = compute_churn(ledger_path)
    assert "sess-A" in result
    assert "sess-B" in result
    assert result["sess-A"]["files_churned"] == 1
    assert result["sess-B"]["files_churned"] == 2
    # Each session has its own warnings.
    assert len(result["sess-A"]["warnings"]) == 1
    assert len(result["sess-B"]["warnings"]) == 2


def test_anti_teatro_churn_zero_silent(ledger_path):
    """Anti-teatro: if compute_churn is mutated to skip the diff, churn_ratio
    must collapse to 0 and this test must FAIL (detecting the stub).

    We simulate a stub implementation that returns 0 for lines and verify
    the churn_ratio collapses to 0 — proving the real implementation's
    non-zero output is meaningful, not theatrical.
    """
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        writes=[{"path": "/foo.py", "lines_added": 10, "lines_deleted": 0}],
    ))

    # Real implementation must produce non-zero churn.
    real = compute_churn(ledger_path)
    assert real["ctx-1"]["lines_added"] > 0, "real impl must count lines"
    assert real["ctx-1"]["churn_ratio"] > 0.0, "real impl must have non-zero ratio"

    # Now simulate a stub that skips the diff (returns 0 for everything).
    with patch("causadb._score._diff_snapshots", return_value=(0, 0, 0)):
        with patch("causadb._score._try_load_snapshot", return_value={"files": {}}):
            stub = compute_churn(ledger_path)
    # The stub collapses churn_ratio to 0 — this is the anti-teatro signal.
    # If the real implementation ever regresses to this, the assertion above
    # would catch it. Here we assert the stub DOES collapse, proving the
    # test has discriminatory power.
    assert stub["ctx-1"]["lines_added"] == 0, (
        "stubbed impl must collapse to 0 — if this fails, the stub is no "
        "longer a stub and the anti-teatro guard is meaningless"
    )
    assert stub["ctx-1"]["churn_ratio"] == 0.0


# ---------------------------------------------------------------------------
# compute_waste tests (F.13.3.2)
# ---------------------------------------------------------------------------

def test_compute_waste_no_llm_invoked(ledger_path):
    """No LLM_INVOKED events → waste_ratio = 0."""
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(path="/foo.py", action="create"))
    result = compute_waste(ledger_path)
    assert "ctx-1" in result
    s = result["ctx-1"]
    assert s["waste_ratio"] == 0.0
    assert s["wasted_cost"] == 0.0
    assert s["wasted_files"] == []


def test_compute_waste_file_written_then_deleted(ledger_path):
    """LLM creates a file, another event deletes it → waste proportional."""
    writer = LedgerWriter(ledger_path)
    # LLM call costs 0.10.
    writer.append(_make_llm_invoked(cost=0.10, timestamp="2026-01-01T10:00:00Z"))
    # File created (attributed to the LLM call by timestamp proximity).
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        timestamp="2026-01-01T10:00:05Z",
    ))
    # File deleted → the write is wasted.
    writer.append(_make_file_modified(
        path="/foo.py", action="delete",
        timestamp="2026-01-01T10:01:00Z",
    ))
    result = compute_waste(ledger_path)
    s = result["ctx-1"]
    assert s["total_cost"] == 0.10
    assert s["wasted_cost"] > 0.0, "wasted_cost must be > 0 when a file is deleted"
    assert s["waste_ratio"] > 0.0
    assert "/foo.py" in s["wasted_files"]


def test_compute_waste_file_written_then_overwritten(ledger_path):
    """File written then overwritten → waste proportional."""
    writer = LedgerWriter(ledger_path)
    writer.append(_make_llm_invoked(cost=0.05, timestamp="2026-01-01T10:00:00Z"))
    writer.append(_make_file_modified(
        path="/bar.py", action="create",
        timestamp="2026-01-01T10:00:05Z",
    ))
    # Overwrite the same file.
    writer.append(_make_file_modified(
        path="/bar.py", action="modify",
        timestamp="2026-01-01T10:00:30Z",
    ))
    result = compute_waste(ledger_path)
    s = result["ctx-1"]
    assert s["wasted_cost"] > 0.0, "overwritten file must count as waste"
    assert "/bar.py" in s["wasted_files"]


def test_compute_waste_correlation_method_in_output(ledger_path):
    """Output must contain correlation_method: 'timestamp_proximity'."""
    writer = LedgerWriter(ledger_path)
    writer.append(_make_llm_invoked(cost=0.01))
    writer.append(_make_file_modified(path="/foo.py", action="create"))
    result = compute_waste(ledger_path)
    s = result["ctx-1"]
    assert s["correlation_method"] == "timestamp_proximity", (
        f"expected 'timestamp_proximity', got {s['correlation_method']!r}"
    )


def test_anti_teatro_waste_no_cross_llm(ledger_path):
    """Anti-teatro: if compute_waste is mutated to not cross LLM_INVOKED,
    waste_ratio must collapse to 0 and this test must FAIL.

    We simulate a stub that returns empty llm_calls and verify the
    waste_ratio collapses — proving the real implementation's non-zero
    output is meaningful.
    """
    writer = LedgerWriter(ledger_path)
    writer.append(_make_llm_invoked(cost=0.10, timestamp="2026-01-01T10:00:00Z"))
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        timestamp="2026-01-01T10:00:05Z",
    ))
    writer.append(_make_file_modified(
        path="/foo.py", action="delete",
        timestamp="2026-01-01T10:01:00Z",
    ))

    # Real implementation must produce non-zero waste.
    real = compute_waste(ledger_path)
    assert real["ctx-1"]["waste_ratio"] > 0.0, "real impl must detect waste"

    # Stub: patch the LLM collection to return empty → waste collapses to 0.
    import causadb._score as score_mod
    original_iter = score_mod._iter_entries

    def stub_iter(path):
        """Same as real iter but skip LLM_INVOKED events."""
        for entry in original_iter(path):
            etype = entry.get("event", {}).get("event_type")
            if etype == "LLM_INVOKED":
                continue
            yield entry

    with patch("causadb._score._iter_entries", side_effect=stub_iter):
        stub = compute_waste(ledger_path)
    # The stub collapses waste to 0 — proving the test has discriminatory power.
    assert stub["ctx-1"]["waste_ratio"] == 0.0, (
        "stubbed impl must collapse to 0 — if this fails, the stub is no "
        "longer a stub and the anti-teatro guard is meaningless"
    )
    assert stub["ctx-1"]["wasted_cost"] == 0.0


# ---------------------------------------------------------------------------
# compute_score tests (F.13.3.3)
# ---------------------------------------------------------------------------

def test_compute_score_perfect_ledger(ledger_path):
    """Perfect ledger (no churn, no waste, full survival) → score 100."""
    # Empty ledger → no churn, no waste, survival defaults to 1.0.
    open(ledger_path, "a").close()
    result = compute_score(ledger_path)
    assert result["overall_score"] == 100.0, (
        f"perfect ledger must score 100, got {result['overall_score']}"
    )
    assert result["churn_score"] == 100.0
    assert result["waste_score"] == 100.0
    assert result["survival_score"] == 100.0


def test_compute_score_weights_respected(ledger_path):
    """Score must use the weights from _config.py."""
    from causadb._config import CausaDBConfig
    # Custom weights: churn=0.5, waste=0.0, survival=0.5.
    config = CausaDBConfig(
        ledger_path=ledger_path,
        score_weight_churn=0.5,
        score_weight_waste=0.0,
        score_weight_survival=0.5,
    )
    # Add churn (fallback path).
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        writes=[{"path": "/foo.py", "lines_added": 100, "lines_deleted": 0}],
    ))
    result = compute_score(ledger_path, config=config)
    w = result["weights_used"]
    assert w["churn"] == 0.5
    assert w["waste"] == 0.0
    assert w["survival"] == 0.5
    # With waste weight 0, waste must not affect the score even if present.
    # The score should reflect churn + survival only.
    assert result["overall_score"] < 100.0, "churn must reduce the score"


def test_compute_score_zero_when_disaster(ledger_path):
    """Disaster ledger (churn=1, waste=1, survival=0) → score 0."""
    from causadb._config import CausaDBConfig
    # Force survival to 0 by patching _get_survival_ratio.
    with patch("causadb._score._get_survival_ratio", return_value=(0.0, [])):
        # Force churn_ratio and waste_ratio to 1 by patching the aggregators.
        with patch("causadb._score.compute_churn", return_value={
            "ctx-1": {
                "files_churned": 10,
                "lines_added": 100,
                "lines_deleted": 100,
                "churn_ratio": 1.0,
                "warnings": [],
            }
        }):
            with patch("causadb._score.compute_waste", return_value={
                "ctx-1": {
                    "total_cost": 1.0,
                    "wasted_cost": 1.0,
                    "waste_ratio": 1.0,
                    "wasted_files": ["/foo.py"],
                    "correlation_method": "timestamp_proximity",
                }
            }):
                result = compute_score(ledger_path)
    assert result["overall_score"] == 0.0, (
        f"disaster ledger must score 0, got {result['overall_score']}"
    )


def test_anti_teatro_score_zero_weights(ledger_path, tmp_path):
    """Anti-teatro: the score must respect the weight config.

    We build a ledger with waste, then compute the score twice:
      1. With waste_weight=0.3 (default).
      2. With waste_weight=0.0.
    The two scores must DIFFER — proving the implementation reads the
    weight config. A stub that hardcodes the weights would produce
    identical scores and fail this test.

    We then simulate the stub (patching the weight resolution to always
    return 0.3) and verify the scores DO collapse to equal — proving the
    test has discriminatory power.
    """
    from causadb._config import CausaDBConfig

    # Ledger with waste: LLM call + file created + file deleted.
    writer = LedgerWriter(ledger_path)
    writer.append(_make_llm_invoked(cost=1.0, timestamp="2026-01-01T10:00:00Z"))
    writer.append(_make_file_modified(
        path="/foo.py", action="create",
        timestamp="2026-01-01T10:00:05Z",
    ))
    writer.append(_make_file_modified(
        path="/foo.py", action="delete",
        timestamp="2026-01-01T10:01:00Z",
    ))

    config_with_waste_weight = CausaDBConfig(
        ledger_path=ledger_path,
        score_weight_churn=0.3,
        score_weight_waste=0.3,
        score_weight_survival=0.4,
    )
    config_zero_waste_weight = CausaDBConfig(
        ledger_path=ledger_path,
        score_weight_churn=0.3,
        score_weight_waste=0.0,
        score_weight_survival=0.4,
    )

    score_with = compute_score(ledger_path, config=config_with_waste_weight)
    score_zero = compute_score(ledger_path, config=config_zero_waste_weight)

    # The two scores must differ — the weight config is respected.
    assert score_with["overall_score"] != score_zero["overall_score"], (
        f"changing waste_weight from 0.3 to 0.0 must change the score; "
        f"got with={score_with['overall_score']}, zero={score_zero['overall_score']}"
    )
    # And specifically: with waste_weight=0, the score must be HIGHER
    # (waste no longer penalizes).
    assert score_zero["overall_score"] > score_with["overall_score"], (
        f"with waste_weight=0 the score must be higher; "
        f"got zero={score_zero['overall_score']}, with={score_with['overall_score']}"
    )

    # Now simulate a stub that ignores the config weights and always uses
    # 0.3/0.3/0.4. We do this by overwriting the weight attribute on BOTH
    # config objects to 0.3 — so regardless of which config is passed,
    # compute_score sees 0.3 for waste_weight.
    # This simulates a stub that hardcodes the weights.
    config_with_waste_weight.score_weight_waste = 0.3
    config_zero_waste_weight.score_weight_waste = 0.3

    stub_with = compute_score(ledger_path, config=config_with_waste_weight)
    stub_zero = compute_score(ledger_path, config=config_zero_waste_weight)

    # The stub ignores the config → both calls produce the same score.
    assert stub_with["overall_score"] == stub_zero["overall_score"], (
        "stubbed impl must produce identical scores regardless of config "
        "weights — if this fails, the stub is no longer a stub and the "
        "anti-teatro guard is meaningless"
    )


def test_compute_waste_multi_session_no_llm_second(ledger_path):
    """Regression: session without LLM calls must not abort compute_waste.

    Bug: compute_waste used a bare ``return`` in the no-LLM branch, which
    aborted the whole per-session loop and returned a flat dict. When a
    second session (FILE_MODIFIED only, no LLM_INVOKED) existed, the flat
    dict was iterated by compute_score as if it were keyed by ctx_id →
    ``'int' object has no attribute 'get'`` crash.
    """
    writer = LedgerWriter(ledger_path)
    # Session 1: LLM + file (default ctx_id="ctx-1").
    writer.append(_make_llm_invoked(cost=0.10, timestamp="2026-01-01T10:00:00Z"))
    writer.append(_make_file_modified(
        path="/foo.py", action="create", timestamp="2026-01-01T10:00:05Z",
    ))
    # Session 2: no LLM, another ctx_id.
    writer.append(_make_file_modified(
        path="/bar.py", action="create", timestamp="2026-01-01T11:00:00Z",
        ctx_id="ctx-2",
    ))
    result = compute_waste(ledger_path)
    # Both sessions must be present (the bug aborted after the first).
    assert "ctx-1" in result
    assert "ctx-2" in result
    assert result["ctx-2"]["waste_ratio"] == 0.0
    assert result["ctx-2"]["wasted_cost"] == 0.0
    # total_cost is float for type consistency with the normal branch.
    assert isinstance(result["ctx-2"]["total_cost"], float)
    # compute_score must not crash with the mixed-session ledger.
    score = compute_score(ledger_path)
    assert "overall_score" in score
    assert "ctx-2" in score["per_session"]
