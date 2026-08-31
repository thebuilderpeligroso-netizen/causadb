import pytest
from causadb._cost_rollup import CostRollup


@pytest.fixture
def sample_events():
    return [
        {"model": "gpt-4", "tokens_in": 100, "tokens_out": 50, "cost": 0.005, "currency": "USD", "ctx_id": "session/abc/step1"},
        {"model": "gpt-4", "tokens_in": 200, "tokens_out": 100, "cost": 0.01, "currency": "USD", "ctx_id": "session/abc/step2"},
        {"model": "claude-3", "tokens_in": 50, "tokens_out": 25, "cost": 0.003, "currency": "USD", "ctx_id": "session/xyz/step1"},
        {"model": "gpt-4", "tokens_in": 30, "tokens_out": 15, "cost": 0.0015, "currency": "USD", "ctx_id": "other"},
    ]


def test_cost_rollup_by_ctx_subtree(sample_events):
    rollup = CostRollup.rollup_by_subtree(sample_events, prefix_depth=2)
    # session/abc → 0.005 + 0.01 = 0.015
    # session/xyz → 0.003
    # other → 0.0015
    assert "session/abc" in rollup
    assert "session/xyz" in rollup
    assert "other" in rollup
    assert abs(rollup["session/abc"] - 0.015) < 0.0001


def test_cost_rollup_empty_returns_empty():
    rollup = CostRollup.rollup_by_subtree([], prefix_depth=2)
    assert rollup == {}


def test_cost_discrepancy_detected():
    result = CostRollup.detect_discrepancy(proxy_cost=0.05, reported_cost=0.03, threshold=0.10)
    assert result is True


def test_cost_discrepancy_within_threshold():
    result = CostRollup.detect_discrepancy(proxy_cost=0.05, reported_cost=0.049, threshold=0.10)
    assert result is False


def test_cost_total_cost_no_filter(sample_events):
    total = CostRollup.total_cost(sample_events)
    expected = 0.005 + 0.01 + 0.003 + 0.0015
    assert abs(total - expected) < 0.0001


def test_cost_total_cost_with_prefix(sample_events):
    total = CostRollup.total_cost(sample_events, ctx_id_prefix="session/abc")
    assert abs(total - 0.015) < 0.0001
