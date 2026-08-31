"""Shared skill filtering utilities (F.13.4.5, Deuda #25).

Provides a single source of truth for filtering skills by session_type
and token budget, used by both `causadb resume` and `causadb revive`.
"""

from typing import List, Dict, Any, Optional


def filter_relevant_skills_from_state(
    state: Dict[str, Any],
    session_type: str,
    max_tokens: int = 2048,
) -> List[Dict[str, Any]]:
    """Filter skills from pre-computed state by session_type and token budget.

    Relevance mapping:
      - abrupt_close → decisions, tool_patterns
      - normal_close → file_tree, conventions
      - first_run → none
      - unknown → all (defensive)

    Args:
        state: Replay state dict containing "skills" list.
        session_type: OCB session type (first_run | abrupt_close | normal_close).
        max_tokens: Token budget for skills context (default 2048).

    Returns:
        Filtered and sorted (timestamp desc) skill list, truncated to max_tokens.
    """
    skills = state.get("skills", []) or []
    if not skills:
        return []

    # Relevance mapping per session_type
    if session_type == "abrupt_close":
        relevant_types = {"decisions", "tool_patterns"}
    elif session_type == "normal_close":
        relevant_types = {"file_tree", "conventions"}
    elif session_type == "first_run":
        return []
    else:
        # Unknown session type — load all (defensive default)
        relevant_types = None

    # Filter by type
    if relevant_types is not None:
        filtered = [s for s in skills if s.get("skill_type") in relevant_types]
    else:
        filtered = list(skills)

    # Sort by timestamp descending (BIT-CHR.103 contract)
    filtered.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    # Truncate to max_tokens budget
    result = []
    total_tokens = 0
    for skill in filtered:
        skill_tokens = skill.get("token_count", 0)
        if total_tokens + skill_tokens > max_tokens:
            allowed = max_tokens - total_tokens
            if allowed > 0:
                content = skill.get("content", "")
                truncated = content[:allowed * 4] + "\n[...]"
                skill_copy = dict(skill)
                skill_copy["content"] = truncated
                skill_copy["token_count"] = allowed
                result.append(skill_copy)
            break
        result.append(skill)
        total_tokens += skill_tokens

    return result
