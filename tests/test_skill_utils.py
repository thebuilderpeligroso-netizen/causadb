"""Tests for shared skill filtering utility (Deuda #25).

Tests verify filter_relevant_skills_from_state() behavior:
- Filters by session_type (abrupt_close, normal_close, first_run)
- Respects max_tokens budget with truncation
- Preserves timestamp desc order (BIT-CHR.103 contract)
- Unknown session_type loads all types (defensive)
"""

import pytest


def _make_skill(skill_type, name, tokens=100, content="sample content", timestamp="2024-01-01T00:00:00Z"):
    """Helper to create a skill dict with all required fields."""
    return {
        "skill_type": skill_type,
        "skill_name": name,
        "content": content,
        "token_count": tokens,
        "confidence": 1.0,
        "source_session": "test",
        "timestamp": timestamp,
    }


class TestFilterRelevantSkillsFromState:
    """Tests for filter_relevant_skills_from_state function."""

    def test_filter_abrupt_close_only_decisions_and_tool_patterns(self):
        """abrupt_close → only decisions and tool_patterns skills."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "dec1"),
                _make_skill("tool_patterns", "tp1"),
                _make_skill("file_tree", "ft1"),
                _make_skill("conventions", "cv1"),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close")

        assert len(result) == 2
        assert all(s["skill_type"] in ["decisions", "tool_patterns"] for s in result)
        types = {s["skill_type"] for s in result}
        assert types == {"decisions", "tool_patterns"}

    def test_filter_normal_close_only_file_tree_and_conventions(self):
        """normal_close → only file_tree and conventions skills."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "dec1"),
                _make_skill("tool_patterns", "tp1"),
                _make_skill("file_tree", "ft1"),
                _make_skill("conventions", "cv1"),
            ]
        }
        result = filter_relevant_skills_from_state(state, "normal_close")

        assert len(result) == 2
        assert all(s["skill_type"] in ["file_tree", "conventions"] for s in result)
        types = {s["skill_type"] for s in result}
        assert types == {"file_tree", "conventions"}

    def test_filter_first_run_returns_empty(self):
        """first_run → no skills (nothing to resume)."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1"),
            ]
        }
        result = filter_relevant_skills_from_state(state, "first_run")

        assert result == []

    def test_filter_respects_max_tokens_truncates(self):
        """Skills exceeding max_tokens budget are truncated with [...]."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", tokens=2500, content="a" * 10000),
                _make_skill("tool_patterns", "tp1", tokens=2500, content="b" * 10000),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close", max_tokens=3000)

        # First skill: 2500 tokens, second would exceed 3000 → truncate second to 500 tokens
        assert len(result) == 2
        assert result[0]["token_count"] == 2500
        assert result[1]["token_count"] == 500
        assert result[1]["content"].endswith("\n[...]")

    def test_filter_preserves_timestamp_desc_order(self):
        """Skills sorted by timestamp descending (BIT-CHR.103 contract)."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", timestamp="2024-01-01T00:00:00Z"),
                _make_skill("decisions", "d2", timestamp="2024-01-02T00:00:00Z"),  # newer
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close")

        # Should be sorted desc (newer first)
        assert result[0]["skill_name"] == "d2"
        assert result[1]["skill_name"] == "d1"

    def test_filter_unknown_session_type_loads_all(self):
        """Unknown session_type → loads all skill types (defensive default)."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1"),
                _make_skill("file_tree", "ft1"),
            ]
        }
        result = filter_relevant_skills_from_state(state, "unknown_type")

        assert len(result) == 2
        types = {s["skill_type"] for s in result}
        assert types == {"decisions", "file_tree"}

    def test_filter_empty_state_returns_empty(self):
        """Empty skills list → empty result."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {"skills": []}
        result = filter_relevant_skills_from_state(state, "abrupt_close")

        assert result == []

    def test_filter_none_skills_returns_empty(self):
        """None skills → empty result."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {"skills": None}
        result = filter_relevant_skills_from_state(state, "abrupt_close")

        assert result == []

    def test_filter_missing_skills_key_returns_empty(self):
        """Missing skills key → empty result."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {}
        result = filter_relevant_skills_from_state(state, "abrupt_close")

        assert result == []

    def test_filter_truncation_preserves_first_skill_fully(self):
        """When truncating, first skill is preserved fully, only subsequent skills truncated."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", tokens=1500, content="x" * 6000),
                _make_skill("tool_patterns", "tp1", tokens=1500, content="y" * 6000),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close", max_tokens=2000)

        # First skill (1500) fits, second (1500) would exceed 2000 → truncate to 500
        assert len(result) == 2
        assert result[0]["token_count"] == 1500
        assert result[0]["content"] == "x" * 6000  # unchanged
        assert result[1]["token_count"] == 500
        assert result[1]["content"].endswith("\n[...]")

    def test_filter_exact_budget_no_truncation(self):
        """Skills exactly matching budget → no truncation."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", tokens=1000),
                _make_skill("tool_patterns", "tp1", tokens=1048),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close", max_tokens=2048)

        assert len(result) == 2
        assert result[0]["token_count"] == 1000
        assert result[1]["token_count"] == 1048
        assert "[...]" not in result[0]["content"]
        assert "[...]" not in result[1]["content"]

    def test_filter_single_skill_exceeds_budget_truncates_it(self):
        """Single skill exceeding budget → truncated to budget."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", tokens=5000, content="z" * 20000),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close", max_tokens=2048)

        assert len(result) == 1
        assert result[0]["token_count"] == 2048
        assert result[0]["content"].endswith("\n[...]")

    def test_filter_zero_budget_returns_empty(self):
        """max_tokens=0 → empty result."""
        from causadb._skill_utils import filter_relevant_skills_from_state

        state = {
            "skills": [
                _make_skill("decisions", "d1", tokens=100),
            ]
        }
        result = filter_relevant_skills_from_state(state, "abrupt_close", max_tokens=0)

        assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])