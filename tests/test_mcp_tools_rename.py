"""Tests for MCP tool rename (F.3 — short names without causadb_ prefix).

Test-First discipline (Article III): these tests were written BEFORE the
implementation. They verify that tools are exposed with short names (e.g.
`log` instead of `causadb_log`) and that the old prefixed names are gone.

Async pattern: Plan B — `anyio.run(...)` inside sync test functions.
"""
import anyio
import pytest

from causadb.mcp.server import create_server


# The complete set of 21 expected tool short names (19 original + 2 shared docs).
EXPECTED_SHORT_NAMES = {
    "log",
    "replay",
    "sentinel",
    "query",
    "validate",
    "feedback",
    "sandbox",
    "stream",
    "impact",
    "why",
    "trace",
    "score",
    "skill_list",
    "log_decision",
    "revive",
    "ocb_status",
    "ocb_load_partition",
    "chronicle_append",
    "recover",
    "shared_document_read",
    "shared_document_write",
}

# The old prefixed names that must NOT appear.
OLD_PREFIXED_NAMES = {
    "causadb_log",
    "causadb_replay",
    "causadb_sentinel",
    "causadb_query",
    "causadb_validate",
    "causadb_feedback",
    "causadb_sandbox",
    "causadb_stream",
    "causadb_impact",
    "causadb_why",
    "causadb_trace",
    "causadb_score",
    "causadb_skill_list",
    "causadb_log_decision",
    "causadb_revive",
    "causadb_ocb_status",
    "causadb_ocb_load_partition",
    "causadb_chronicle_append",
    "causadb_recover",
    "causadb_shared_document_read",
    "causadb_shared_document_write",
}


def _tool_names(server):
    """Return a set of tool names from the server's list_tools()."""
    async def _run():
        tools = await server.list_tools()
        return {t.name for t in tools}
    return anyio.run(_run)


# ---------------------------------------------------------------------------
# 1. All tools have short names
# ---------------------------------------------------------------------------

def test_mcp_tools_have_short_names():
    """Every tool must have a short name (no `causadb_` prefix).

    Anti-teatro: a stub that keeps the old `causadb_` prefix would fail
    because the short names would not match the expected set.

    A tool with a non-standard name (e.g. `causadb_extra`) would also fail
    the exact set match.
    """
    server = create_server()
    names = _tool_names(server)

    # First check that NO name starts with "causadb_"
    for name in names:
        assert not name.startswith("causadb_"), (
            f"Tool name {name!r} still has causadb_ prefix"
        )

    # Then check that the set matches exactly
    assert names == EXPECTED_SHORT_NAMES, (
        f"Tool name mismatch.\n"
        f"  Unexpected: {names - EXPECTED_SHORT_NAMES}\n"
        f"  Missing:    {EXPECTED_SHORT_NAMES - names}\n"
        f"  Got:        {sorted(names)}"
    )
    assert len(names) == 21, (
        f"expected exactly 21 tools, got {len(names)}: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# 2. Anti-teatro: old prefix names are gone
# ---------------------------------------------------------------------------

def test_anti_teatro_old_names_gone():
    """No tool may be exposed with the old `causadb_` prefix.

    Anti-teatro: if someone accidentally registers a tool with the old
    prefixed name (e.g. by forgetting to rename a @mcp.tool() decorator),
    this test will fail because the old name would appear in the listing
    AND the short name would NOT appear.

    We verify both that old names are absent AND that short names are
    present (covers both failure modes).
    """
    server = create_server()
    names = _tool_names(server)

    # Old names must be completely absent
    present_old = names & OLD_PREFIXED_NAMES
    assert not present_old, (
        f"Old prefixed tool names are still present: {present_old}"
    )

    # Short names must be present
    missing_short = EXPECTED_SHORT_NAMES - names
    assert not missing_short, (
        f"Short tool names are missing: {missing_short}"
    )
