"""RED tests for snapshot-path churn ratio (BIT fix: denom=max per modified file).

Specs (auditadas):
- denominador por archivo modificado = max(pre_size, post_size)
- agregados/borrados igual que ahora
- guardia denom==0 -> 0.0
- 100->99 = deleted 1, ratio 0.01 ; 100->150 = 0.0 ; growth-solo 0.0 correcto
- no tocar path `writes`
"""
import pytest

from causadb._score import compute_churn, compute_score, _diff_snapshots
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._blob_store import BlobStore


@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


def _store_snapshot(tmp_path, files):
    store = BlobStore(str(tmp_path / "blobs"))
    snap = {"type": "snapshot", "files": files}
    h = store.put(snap)
    return h


def _make_file_modified(ctx_id="ctx-1", path="a.py", action="modify",
                        pre_snapshot=None, post_snapshot=None):
    return CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id=ctx_id,
        source="opencode:agent",
        source_type="agent",
        payload={"path": path, "action": action},
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
    )


def test_snapshot_shrink_ratio_is_proportional(ledger_path, tmp_path):
    """100->99 via snapshots: deleted 1, ratio ~= 0.01 (NO 1.0)."""
    pre = {"a.py": {"hash": "h1", "size": 100, "mtime": 1}}
    post = {"a.py": {"hash": "h2", "size": 99, "mtime": 2}}
    pre_h = _store_snapshot(tmp_path, pre)
    post_h = _store_snapshot(tmp_path, post)
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(pre_snapshot=pre_h, post_snapshot=post_h))
    result = compute_churn(ledger_path)
    s = result["ctx-1"]
    assert s["lines_added"] == 0
    assert s["lines_deleted"] == 1
    assert s["churn_ratio"] == pytest.approx(0.01), (
        f"100->99 must give ratio 0.01, got {s['churn_ratio']}"
    )
    assert s["churn_ratio"] != pytest.approx(1.0)


def test_snapshot_growth_ratio_is_zero(ledger_path, tmp_path):
    """100->150 via snapshots: added 50, deleted 0, ratio == 0.0."""
    pre = {"a.py": {"hash": "h1", "size": 100, "mtime": 1}}
    post = {"a.py": {"hash": "h2", "size": 150, "mtime": 2}}
    pre_h = _store_snapshot(tmp_path, pre)
    post_h = _store_snapshot(tmp_path, post)
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(pre_snapshot=pre_h, post_snapshot=post_h))
    result = compute_churn(ledger_path)
    s = result["ctx-1"]
    assert s["lines_added"] == 50
    assert s["lines_deleted"] == 0
    assert s["churn_ratio"] == 0.0


def test_snapshot_ledger_aggregation_uses_denom(ledger_path, tmp_path):
    """Ledger-level aggregation con snapshots: ponderado por denom.

    sess-A: 100->99 (deleted 1, denom 100, ratio 0.01)
    sess-B: 100->150 (deleted 0, denom 150, ratio 0.0)
    total deleted 1, total denom 250 -> churn_ratio 1/250, score 99.6.
    """
    # sess-A shrink
    pre_a = {"a.py": {"hash": "h1", "size": 100, "mtime": 1}}
    post_a = {"a.py": {"hash": "h2", "size": 99, "mtime": 2}}
    pre_ha = _store_snapshot(tmp_path, pre_a)
    post_ha = _store_snapshot(tmp_path, post_a)
    # sess-B growth (distintas hashes para no colisionar blobs)
    pre_b = {"b.py": {"hash": "g1", "size": 100, "mtime": 1}}
    post_b = {"b.py": {"hash": "g2", "size": 150, "mtime": 2}}
    pre_hb = _store_snapshot(tmp_path, pre_b)
    post_hb = _store_snapshot(tmp_path, post_b)
    writer = LedgerWriter(ledger_path)
    writer.append(_make_file_modified(
        ctx_id="sess-A", path="a.py",
        pre_snapshot=pre_ha, post_snapshot=post_ha))
    writer.append(_make_file_modified(
        ctx_id="sess-B", path="b.py",
        pre_snapshot=pre_hb, post_snapshot=post_hb))
    churn = compute_churn(ledger_path)
    assert churn["sess-A"]["churn_ratio"] == pytest.approx(0.01)
    assert churn["sess-B"]["churn_ratio"] == 0.0
    score = compute_score(ledger_path)
    assert score["per_session"]["sess-A"]["churn_ratio"] == pytest.approx(0.01)
    assert score["per_session"]["sess-B"]["churn_ratio"] == 0.0
    assert score["churn_score"] == pytest.approx(100.0 * (1.0 - 1 / 250)), (
        f"ledger churn_score must weight by denom (1/250), got {score['churn_score']}"
    )
