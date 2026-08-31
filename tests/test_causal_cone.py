"""Tests for `causadb._causal_cone` (F.12.4 — downstream cone / impact).

Test-First discipline (Article III): these tests were written BEFORE the
implementation of `trace_downstream`. They build synthetic ledgers in
`tmp_path` using the real `LedgerWriter` (Article I — Ledger Monism) and
assert on the downstream causal cone returned by `trace_downstream`.

Payload contract (agent-declared file dependencies):
  - ``writes``: list[str] of file paths the event mutated.
  - ``reads``:  list[str] of file paths the event read.

A downstream event is *tainted* iff it reads a file that was written by
the source event OR by any previously-tainted event (transitive closure).

Anti-teatro (Article IX): the last test mutates `trace_downstream` to
stop after the first hop (no transitive taint) and asserts the
transitive test contract breaks — proving the propagation step is
actually exercised. The mutation is RESTORED in a `try/finally`.

NOTE: F.12.3 will later ADD `trace_upstream` tests to this same file.
The structure below leaves room for that — downstream tests are grouped
under a clearly delimited section header so upstream tests can be
appended without conflict.
"""
import json
import os
import time

import pytest

from causadb._causal_cone import trace_downstream, trace_upstream
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig


# ---------------------------------------------------------------------------
# Helpers — build a synthetic ledger with the real LedgerWriter
# ---------------------------------------------------------------------------

def _make_ledger(tmp_path):
    """Return (ledger_path, writer) for a fresh empty ledger."""
    ledger = str(tmp_path / "ledger.log")
    config = CausaDBConfig(ledger_path=ledger)
    writer = LedgerWriter(ledger, config=config)
    return ledger, writer


def _log_event(writer, event_type=EventType.FILE_MODIFIED, ctx_id="ctx",
               source="causadb:test", source_type="agent",
               writes=None, reads=None, event_id=None,
               parent_event_id=None):
    """Append a single event with the given file deps in its payload."""
    payload = {}
    if writes is not None:
        payload["writes"] = list(writes)
    if reads is not None:
        payload["reads"] = list(reads)
    event = CanonicalEvent(
        event_type=event_type,
        ctx_id=ctx_id,
        source=source,
        source_type=source_type,
        payload=payload,
        parent_event_id=parent_event_id,
    )
    if event_id is not None:
        object.__setattr__(event, "event_id", event_id)
    writer.append(event)
    return event.event_id


# ===========================================================================
# F.12.4 — Downstream cone (trace_downstream)
# ---------------------------------------------------------------------------
# F.12.3 will append `trace_upstream` tests below this section.
# ===========================================================================


def test_impact_returns_events_that_read_written_files(tmp_path):
    """A writes main.py; B reads main.py → trace_downstream(A) includes B."""
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py"])

    result = trace_downstream(a_id, ledger)

    assert isinstance(result, list)
    tainted_ids = {e["event_id"] for e in result}
    assert b_id in tainted_ids, (
        f"B reads main.py written by A, so B must be in the downstream cone; "
        f"got {tainted_ids}"
    )
    assert a_id not in tainted_ids, "source event must not taint itself"


def test_impact_transitive(tmp_path):
    """A writes main.py; B reads main.py + writes utils.py; C reads utils.py
    → trace_downstream(A) includes BOTH B and C (transitive taint)."""
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py"], writes=["utils.py"])
    c_id = _log_event(writer, reads=["utils.py"])

    result = trace_downstream(a_id, ledger)

    tainted_ids = {e["event_id"] for e in result}
    assert b_id in tainted_ids, "B reads main.py (written by A) → tainted"
    assert c_id in tainted_ids, (
        "C reads utils.py (written by tainted B) → transitively tainted"
    )


def test_impact_no_deps_returns_empty(tmp_path):
    """An event whose writes nobody reads → empty downstream cone."""
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(writer, writes=["lonely.py"])
    # Unrelated events that don't touch lonely.py.
    _log_event(writer, writes=["other.py"])
    _log_event(writer, reads=["other.py"])

    result = trace_downstream(a_id, ledger)

    assert result == [], (
        f"no downstream event reads lonely.py, expected empty list, got {result}"
    )


def test_impact_works_with_manual_events(tmp_path):
    """Events logged by hand (source_type='human', no parent_event_id) with
    explicit reads/writes in the payload are traced correctly."""
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(
        writer,
        event_type=EventType.COMMAND_RUN,
        source="human:operator",
        source_type="human",
        writes=["config.yml"],
    )
    b_id = _log_event(
        writer,
        event_type=EventType.TOOL_CALLED,
        source="human:operator",
        source_type="human",
        reads=["config.yml"],
        writes=["out.txt"],
    )

    result = trace_downstream(a_id, ledger)

    tainted_ids = {e["event_id"] for e in result}
    assert b_id in tainted_ids, (
        "manual event B reads config.yml written by manual event A → tainted"
    )


# ---------------------------------------------------------------------------
# Anti-teatro (Article IX)
# ---------------------------------------------------------------------------

def test_anti_teatro_impact_skips_propagation(tmp_path):
    """Mutate `trace_downstream` to stop after the first hop (no transitive
    taint) → the transitive test contract breaks (C missing, only B returned).
    Then RESTORE the original implementation and verify exact equality.

    This proves the propagation step is actually exercised — a stub that
    only processes the immediate downstream of the source (one hop) would
    return [B] and miss C, failing `test_impact_transitive`.
    """
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(writer, writes=["main.py"])
    b_id = _log_event(writer, reads=["main.py"], writes=["utils.py"])
    c_id = _log_event(writer, reads=["utils.py"])

    # Sanity: real implementation returns BOTH B and C.
    real_result = trace_downstream(a_id, ledger)
    real_ids = {e["event_id"] for e in real_result}
    assert real_ids == {b_id, c_id}, (
        f"real trace_downstream must return {{B, C}}, got {real_ids}"
    )

    # --- MUTATION: one-hop stub (no transitive propagation) ---
    import causadb._causal_cone as cone_mod
    original = cone_mod.trace_downstream

    def _stub_one_hop(event_id, ledger_path):
        events = list(cone_mod._iter_events_from(ledger_path))
        source_writes = cone_mod._effective_writes_for(events, event_id)
        tainted = []
        started = False
        for ev in events:
            if not started:
                if ev["event_id"] == event_id:
                    started = True
                continue
            reads = set(ev.get("payload", {}).get("reads", []) or [])
            if reads & source_writes:
                tainted.append(ev)
        return tainted

    cone_mod.trace_downstream = _stub_one_hop
    try:
        mutated_result = cone_mod.trace_downstream(a_id, ledger)
        mutated_ids = {e["event_id"] for e in mutated_result}
        # One-hop stub returns ONLY B (immediate downstream of A).
        assert mutated_ids == {b_id}, (
            "mutated one-hop stub must return only B — otherwise the "
            "anti-teatro check is vacuous"
        )
        # The transitive contract (`C in result`) BREAKS under the mutation.
        assert c_id not in mutated_ids, (
            "mutation must break the transitive contract: C must be missing"
        )
    finally:
        # --- RESTORE ---
        cone_mod.trace_downstream = original

    # After restore, the real implementation works again and matches exactly.
    restored_result = trace_downstream(a_id, ledger)
    restored_ids = {e["event_id"] for e in restored_result}
    assert restored_ids == real_ids, "restore must be exact (same tainted set)"


# ===========================================================================
# F.12.3 — Upstream cone (trace_upstream)
# ---------------------------------------------------------------------------
# Given `main.py:42`, find ALL events that transitively contributed to that
# line via files-read → files-written. The full causal tree upstream.
#
# Algorithm (plan lines 281-292):
#   1. Find writer event W that introduced the line (via attribute_line).
#   2. Build the full chain oldest-first from HEAD.
#   3. Build writer_history: file → [(event_id, chain_position)] sorted by
#      chain_position.
#   4. BFS from W: for each event E, compute effective_reads(E) = declared
#      reads + writes of E. For each file F read by E, binary search
#      (bisect) writer_history[F] for the last writer with chain_position
#      STRICTLY LESS than E's position. If found, add edge (F, prior) to
#      upstream and enqueue prior if not already in cone.
#   5. Return tree with depth and visited-set.
# ===========================================================================


def test_trace_returns_writer_event_first(tmp_path):
    """trace of main.py:42 → first node is the writer event W.

    The writer event is the event that introduced the line (via
    `attribute_line` from F.12.2). The cone's `writer_event` field must
    match it, and the cone's `visited` set must contain it.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Event 1: introduce line 1 of main.py ("x = 1").
    w_id = _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="w", reasoning="introduce line 1",
    )

    result = trace_upstream("main.py", 1, ledger)

    introducer = attribute_line("main.py", 1, ledger)
    assert introducer is not None, "attribute_line must find the writer event"
    assert result["writer_event"]["event_id"] == introducer["event_id"], (
        "trace_upstream's writer_event must match attribute_line's introducer"
    )
    assert introducer["event_id"] == w_id, (
        "the writer event must be the event that introduced line 1"
    )
    assert w_id in result["visited"], (
        "the writer event must be in the visited set"
    )


def test_trace_includes_reads_upstream(tmp_path):
    """Writer W read spec.md → trace includes the event that wrote spec.md.

    Setup:
      - ev_root: writes spec.md (no reads).
      - W: reads spec.md, writes main.py (introduces line 1).
    trace_upstream(main.py, 1) must include ev_root in the cone (1 hop
    upstream via the spec.md read).
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev_root: write spec.md (introduce it).
    _log_event_with_snapshot(
        writer, store, ws, "spec.md",
        pre_present=False, pre_content="",
        post_present=True, post_content="# spec\n",
        prompt="root", reasoning="write spec",
        reads=[], writes=["spec.md"],
    )
    root_id = _last_event_id(ledger)

    # W: read spec.md, write main.py (introduce line 1).
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="w", reasoning="read spec, write main",
        reads=["spec.md"], writes=["main.py"],
        parent_event_id=root_id,
    )
    w_id = _last_event_id(ledger)

    result = trace_upstream("main.py", 1, ledger)

    assert result["writer_event"]["event_id"] == w_id
    cone_ids = set(result["cone"].keys())
    assert root_id in cone_ids, (
        f"ev_root (wrote spec.md, which W read) must be in the upstream cone; "
        f"got cone_ids={cone_ids}"
    )
    # The edge (spec.md, root_id) must be in W's upstream.
    w_upstream_files = {edge[0] for edge in result["cone"][w_id]["upstream"]}
    w_upstream_ids = {edge[1] for edge in result["cone"][w_id]["upstream"]}
    assert "spec.md" in w_upstream_files, (
        f"W read spec.md, so (spec.md, root_id) must be an upstream edge; "
        f"got upstream={result['cone'][w_id]['upstream']}"
    )
    assert root_id in w_upstream_ids


def test_trace_transitive(tmp_path):
    """Transitive: W read spec.md, spec.md was written by event that read
    config.json → trace includes the config.json event (2 hops).

    Setup:
      - ev_cfg: writes config.json (no reads).
      - ev_spec: reads config.json, writes spec.md.
      - W: reads spec.md, writes main.py (introduces line 1).
    trace_upstream(main.py, 1) must include BOTH ev_spec (1 hop) and
    ev_cfg (2 hops, transitive).
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev_cfg: write config.json.
    _log_event_with_snapshot(
        writer, store, ws, "config.json",
        pre_present=False, pre_content="",
        post_present=True, post_content='{"k": 1}\n',
        prompt="cfg", reasoning="write config",
        reads=[], writes=["config.json"],
    )
    cfg_id = _last_event_id(ledger)

    # ev_spec: read config.json, write spec.md.
    _log_event_with_snapshot(
        writer, store, ws, "spec.md",
        pre_present=False, pre_content="",
        post_present=True, post_content="# spec\n",
        prompt="spec", reasoning="read config, write spec",
        reads=["config.json"], writes=["spec.md"],
        parent_event_id=cfg_id,
    )
    spec_id = _last_event_id(ledger)

    # W: read spec.md, write main.py (introduce line 1).
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="w", reasoning="read spec, write main",
        reads=["spec.md"], writes=["main.py"],
        parent_event_id=spec_id,
    )
    w_id = _last_event_id(ledger)

    result = trace_upstream("main.py", 1, ledger)

    cone_ids = set(result["cone"].keys())
    assert w_id in cone_ids, "W must be in the cone"
    assert spec_id in cone_ids, (
        f"ev_spec (wrote spec.md, which W read) must be in the cone (1 hop); "
        f"got cone_ids={cone_ids}"
    )
    assert cfg_id in cone_ids, (
        f"ev_cfg (wrote config.json, which ev_spec read) must be in the cone "
        f"(2 hops, transitive); got cone_ids={cone_ids}"
    )
    assert result["depth"] >= 2, (
        f"transitive trace must reach depth >= 2; got depth={result['depth']}"
    )


def test_trace_stops_at_root(tmp_path):
    """Root event (no parent, no reads) → trace terminates there.

    Setup:
      - ev_root: writes main.py (introduces line 1), no reads, no parent.
    trace_upstream(main.py, 1) must return a cone with ONLY ev_root (the
    writer is also the root), depth 0, no upstream edges.
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="root", reasoning="root write, no reads",
        reads=[], writes=["main.py"],
    )
    root_id = _last_event_id(ledger)

    result = trace_upstream("main.py", 1, ledger)

    assert result["writer_event"]["event_id"] == root_id
    cone_ids = set(result["cone"].keys())
    assert cone_ids == {root_id}, (
        f"root event with no reads → cone must be {{root}}, got {cone_ids}"
    )
    assert result["cone"][root_id]["upstream"] == [], (
        "root event must have no upstream edges"
    )
    assert result["depth"] == 0


def test_trace_no_reads_returns_just_writer(tmp_path):
    """Writer without reads → trace = {writer} only.

    Setup:
      - ev_other: writes other.py (unrelated, no reads).
      - W: writes main.py (introduces line 1), no reads.
    trace_upstream(main.py, 1) must return a cone with ONLY W.
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Unrelated event.
    _log_event_with_snapshot(
        writer, store, ws, "other.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="y = 2\n",
        prompt="other", reasoning="unrelated",
        reads=[], writes=["other.py"],
    )

    # W: write main.py, no reads.
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="w", reasoning="no reads",
        reads=[], writes=["main.py"],
    )
    w_id = _last_event_id(ledger)

    result = trace_upstream("main.py", 1, ledger)

    cone_ids = set(result["cone"].keys())
    assert cone_ids == {w_id}, (
        f"writer with no reads → cone must be {{writer}}, got {cone_ids}"
    )
    assert result["cone"][w_id]["upstream"] == []


def test_anti_teatro_trace_skips_reads(tmp_path):
    """Mutate `trace_upstream` to NOT process reads (return just W and its
    cone without exploring reads) → the reads-upstream contract breaks
    (ev_root missing from the cone when it should be present). Then RESTORE
    in try/finally with a post-restore equality check.

    Anti-teatro (Article IX): a stub that returns only the writer event
    without exploring its reads would miss ev_root, failing
    `test_trace_includes_reads_upstream`'s contract. This proves the
    reads-processing step is actually exercised.
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev_root: write spec.md.
    _log_event_with_snapshot(
        writer, store, ws, "spec.md",
        pre_present=False, pre_content="",
        post_present=True, post_content="# spec\n",
        prompt="root", reasoning="write spec",
        reads=[], writes=["spec.md"],
    )
    root_id = _last_event_id(ledger)

    # W: read spec.md, write main.py (introduce line 1).
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="w", reasoning="read spec, write main",
        reads=["spec.md"], writes=["main.py"],
        parent_event_id=root_id,
    )
    w_id = _last_event_id(ledger)

    # Sanity: real implementation includes ev_root in the cone.
    real_result = trace_upstream("main.py", 1, ledger)
    real_cone_ids = set(real_result["cone"].keys())
    assert root_id in real_cone_ids, (
        f"real trace_upstream must include ev_root; got {real_cone_ids}"
    )

    # --- MUTATION: stub that skips reads processing (returns just W) ---
    import causadb._causal_cone as cone_mod
    original = cone_mod.trace_upstream

    def _stub_skip_reads(file_path, line_number, ledger_path):
        from causadb._causal_attrib import attribute_line
        introducer = attribute_line(file_path, line_number, ledger_path)
        if introducer is None:
            return {
                "writer_event": None,
                "cone": {},
                "visited": set(),
                "depth": 0,
            }
        w_eid = introducer["event_id"]
        return {
            "writer_event": introducer,
            "cone": {w_eid: {"reason": "writer", "upstream": []}},
            "visited": {w_eid},
            "depth": 0,
        }

    cone_mod.trace_upstream = _stub_skip_reads
    try:
        mutated_result = cone_mod.trace_upstream("main.py", 1, ledger)
        mutated_cone_ids = set(mutated_result["cone"].keys())
        # The skip-reads stub returns ONLY W (no upstream exploration).
        assert mutated_cone_ids == {w_id}, (
            "mutated skip-reads stub must return only W — otherwise the "
            "anti-teatro check is vacuous"
        )
        # The reads-upstream contract (`ev_root in cone`) BREAKS under the
        # mutation.
        assert root_id not in mutated_cone_ids, (
            "mutation must break the reads-upstream contract: ev_root must "
            "be missing from the skip-reads stub's cone"
        )
    finally:
        # --- RESTORE ---
        cone_mod.trace_upstream = original

    # After restore, the real implementation works again and matches exactly.
    restored_result = trace_upstream("main.py", 1, ledger)
    restored_cone_ids = set(restored_result["cone"].keys())
    assert restored_cone_ids == real_cone_ids, (
        "restore must be exact (same cone event-id set)"
    )
    assert root_id in restored_cone_ids, (
        "restored impl must include ev_root again"
    )


# ---------------------------------------------------------------------------
# TD-#3a cone — trace_upstream works without parent_event_id links
# ---------------------------------------------------------------------------
# BIT-CHR.110 TD-#3a (capa cone): en el ledger real el harvester NO setea
# ``parent_event_id`` (~99.99% None), así que la cadena oldest-first
# construida vía parents se reduce a [HEAD] y writer_history pierde a
# todos los writers previos → trace_upstream no encuentra upstream.
# El fix usa la POSICIÓN EN ORDEN DE LEDGER (enumerate) como
# ``chain_position`` — un orden total presente siempre, con o sin
# parents. Anti-teatro: ev1 es el writer previo de main.py — si el cone
# solo tuviera {ev2}, el trace no habría caminado por la posición.

def test_trace_upstream_works_without_parent_links(tmp_path):
    """No parent_event_id on ANY event — writer_history must still index
    BOTH writers of main.py via ledger-order positions.

    Setup:
      - ev1: writes main.py (introduces line 1). NO parent.
      - ev2: writes main.py (introduces line 2). NO parent. HEAD = ev2.
    trace_upstream(main.py, 2) → writer = ev2, cone must INCLUDE ev1
    (the previous writer of main.py, found via ledger-order position).
    """
    ws, store, config, ledger = _make_workspace_with_snapshots(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev1: introduce main.py line 1. NO parent.
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
        reads=[], writes=["main.py"],
    )
    ev1_id = _last_event_id(ledger)

    # ev2: append main.py line 2. NO parent. HEAD = ev2.
    _log_event_with_snapshot(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        prompt="ev2", reasoning="second",
        reads=[], writes=["main.py"],
    )
    ev2_id = _last_event_id(ledger)

    result = trace_upstream("main.py", 2, ledger)

    # The writer (introducer of line 2) is ev2.
    assert result["writer_event"]["event_id"] == ev2_id, (
        f"line 2 was introduced by ev2, got "
        f"{result['writer_event'].get('event_id')}"
    )
    cone_ids = set(result["cone"].keys())
    assert ev2_id in cone_ids, "writer (ev2) must be in the cone"
    # ev1 is the previous writer of main.py — with ledger-order positions
    # (no parent chain needed) it must appear as upstream of ev2.
    assert ev1_id in cone_ids, (
        f"ev1 (previous writer of main.py) must be in the upstream cone "
        f"even without parent links; got cone_ids={cone_ids}"
    )
    # Anti-teatro: the upstream edge (main.py, ev1) must exist on ev2.
    ev2_upstream_ids = {edge[1] for edge in result["cone"][ev2_id]["upstream"]}
    assert ev1_id in ev2_upstream_ids, (
        f"ev2's upstream must include ev1 via main.py; got "
        f"upstream={result['cone'][ev2_id]['upstream']}"
    )


def test_trace_performance_1000_events(tmp_path):
    """1000 events with mixed reads/writes → trace completes in < 1 second.

    Verifies the bisect-driven O(n log n) BFS. A naive O(n²) linear scan
    over writer_history for each event would blow up on 1000 events.

    Setup: a chain of 1000 events where each event reads the file written
    by the previous event (so the upstream cone is the full chain). The
    trace must complete in well under 1 second.

    NOTE: the ledger is built with the lightweight `_log_event` helper (no
    workspace snapshots — only reads/writes in the payload). `attribute_line`
    requires snapshots to find the writer, so we stub it to return the HEAD
    event (the last event in the chain) as the writer. This isolates the
    performance measurement to the trace BFS itself, which is the subject
    of the O(n log n) bisect claim.
    """
    import causadb._causal_cone as cone_mod
    import causadb._causal_attrib as attrib_mod

    ledger, writer = _make_ledger(tmp_path)

    # ev0: write file_0.py (root, no reads).
    prev_id = _log_event(writer, writes=["file_0.py"])
    # ev1..ev999: each reads file_{i-1}.py, writes file_i.py.
    for i in range(1, 1000):
        prev_id = _log_event(
            writer,
            reads=[f"file_{i-1}.py"],
            writes=[f"file_{i}.py"],
            parent_event_id=prev_id,
        )

    # The HEAD event (last appended) is the writer we trace from.
    head_id = prev_id

    # Stub attribute_line to return the HEAD event as the writer — avoids
    # the snapshot requirement (which is not what this test measures).
    original_attr = cone_mod.attribute_line if hasattr(cone_mod, "attribute_line") else None
    # trace_upstream imports attribute_line lazily inside the function body,
    # so patching the module attribute is sufficient.
    def _stub_attribute_line(file_path, line_number, ledger_path):
        from causadb._ledger_reader import LedgerReader
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    # Patch the lazy import target. trace_upstream does
    # `from causadb._causal_attrib import attribute_line`, so we patch the
    # attribute on the _causal_attrib module.
    original_attrib_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        start = time.time()
        result = trace_upstream("file_999.py", 1, ledger)
        elapsed = time.time() - start
    finally:
        attrib_mod.attribute_line = original_attrib_attr

    assert elapsed < 1.0, (
        f"trace of 1000-event chain took {elapsed:.3f}s, expected < 1.0s "
        f"(bisect-driven O(n log n) should be fast)"
    )
    # Sanity: the cone includes the writer and at least the root.
    cone_ids = set(result["cone"].keys())
    assert len(cone_ids) >= 2, (
        f"chain trace must include >= 2 events, got {len(cone_ids)}"
    )
    assert head_id in cone_ids, "the writer (HEAD) must be in the cone"


# ---------------------------------------------------------------------------
# Helpers for upstream tests (snapshot-based, since attribute_line needs
# pre/post snapshots to find the writer event).
# ---------------------------------------------------------------------------

def _make_workspace_with_snapshots(tmp_path, name="ws"):
    """Create a workspace + BlobStore + CausaDBConfig wired for snapshots.

    Mirrors `test_causal_attrib._make_workspace` so `attribute_line` (used
    internally by `trace_upstream` to find the writer event) can resolve
    snapshots from the blob store rooted next to the ledger.
    """
    from causadb._blob_store import BlobStore
    ws = str(tmp_path / name)
    os.makedirs(ws, exist_ok=True)
    ledger = str(tmp_path / "ledger.log")
    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=str(tmp_path / "blobs"),
    )
    config.workspace_dir = ws
    os.makedirs(config.blob_store_path, exist_ok=True)
    store = BlobStore(config.blob_store_path)
    return ws, store, config, ledger


def _log_event_with_snapshot(
    writer, store, ws, rel_path,
    pre_present, pre_content,
    post_present, post_content,
    source="opencode:agent1",
    source_type="agent",
    prompt=None, reasoning=None,
    reads=None, writes=None,
    parent_event_id=None,
    event_type=EventType.FILE_MODIFIED,
    ctx_id="ctx",
):
    """Build a deterministic event with explicit pre/post snapshots AND
    agent-declared reads/writes in the payload.

    The reads/writes are the file-dependency contract used by the causal
    cone; the snapshots are used by `attribute_line` to find the writer.
    ``writes`` defaults to ``[rel_path]`` so ``LedgerWriter`` propagates
    the snapshot hashes onto the event's top-level ``pre_snapshot`` /
    ``post_snapshot`` fields (required by ``attribute_line``).
    """
    from types import MappingProxyType
    from causadb._snapshot import WorkspaceSnapshot

    target = os.path.join(ws, rel_path)

    # pre snapshot
    if pre_present:
        with open(target, "w") as f:
            f.write(pre_content)
    elif os.path.exists(target):
        os.remove(target)
    pre_snap = WorkspaceSnapshot.take(ws)
    pre_hash = WorkspaceSnapshot.store(pre_snap, store, root_dir=ws)

    # post snapshot
    if post_present:
        with open(target, "w") as f:
            f.write(post_content)
    elif os.path.exists(target):
        os.remove(target)
    post_snap = WorkspaceSnapshot.take(ws, prev_snapshot=pre_snap)
    post_hash = WorkspaceSnapshot.store(post_snap, store, root_dir=ws)

    if writes is None:
        writes = [rel_path]
    payload = {
        "action": "modified",
        "path": target,
        "pre_snapshot": pre_hash,
        "post_snapshot": post_hash,
        "writes": list(writes),
    }
    if reads is not None:
        payload["reads"] = list(reads)
    if prompt is not None:
        payload["prompt"] = prompt
    if reasoning is not None:
        payload["reasoning"] = reasoning

    event = CanonicalEvent(
        event_type=event_type,
        ctx_id=ctx_id,
        source=source,
        source_type=source_type,
        payload=MappingProxyType(payload),
        parent_event_id=parent_event_id,
    )
    writer.append(event)
    return event.event_id


def _last_event_id(ledger_path):
    """Return the event_id of the last entry in the ledger."""
    from causadb._ledger_reader import LedgerReader
    reader = LedgerReader(ledger_path)
    events = list(reader.read_all())
    assert events, "ledger must have at least one event"
    return events[-1].event_id


# ===========================================================================
# F.13.1.5 — Integración DAG cache en trace_upstream / trace_downstream
# ---------------------------------------------------------------------------
# Estos tests verifican que ``trace_upstream`` y ``trace_downstream`` usan
# el DAG cache (``.causadb/dag.json``) cuando está fresh, y caen a full-scan
# on-the-fly cuando está stale, corrupto, o below threshold.
#
# Anti-teatro (Artículo IX): el test #7 construye un DAG stale (con eventos
# nuevos en el ledger NO incluidos en el cache) y verifica que trace
# NO usa el cache stale — si lo usara, el resultado sería incorrecto
# (faltan eventos) y el test fallaría. Esto prueba que la verificación
# ``is_dag_stale`` realmente se ejecuta.
# ===========================================================================


def _dag_path_for(ledger_path):
    """Path del DAG cache asociado al ledger — delega a la función de
    producción ``_causal_cone._dag_path_for`` (BIT-CHR.117: el DAG vive
    en el MISMO directorio que el ledger, no en ``.causadb/`` anidado)."""
    from causadb._causal_cone import _dag_path_for as _prod_dag_path_for
    return _prod_dag_path_for(ledger_path)


def _build_and_write_dag(ledger_path):
    """Construye el DAG cache desde el ledger actual y lo escribe a disco.

    Usa ``build_dag`` + ``write_dag`` de ``_dag_cache`` y setea los
    campos de metadata que ``_load_or_build_dag`` setea en producción
    (``last_offset``, ``last_hash``, ``covered_archives``) para que el
    cache escrito sea realista (el tail-read incremental depende de
    ``last_offset``). El DAG resultante está fresh respecto al ledger.
    """
    from causadb._dag_cache import build_dag, write_dag
    from causadb._ledger_reader import LedgerReader
    from causadb._causal_cone import _dag_path_for, _last_complete_line_end

    reader = LedgerReader(ledger_path)
    events = [entry["event"] for entry in reader.read_all_entries()]
    dag = build_dag(events)
    dag["last_offset"] = _last_complete_line_end(ledger_path)
    dag["last_hash"] = _read_last_hash_file(ledger_path)
    dag["covered_archives"] = _current_archives_list(ledger_path)
    dag_path = _dag_path_for(ledger_path)
    write_dag(dag, dag_path)
    return dag_path


def _read_last_hash_file(ledger_path):
    """Lee ``<ledger>.last_hash.json`` → str, o "" si no existe."""
    import json as _json
    last_hash_path = ledger_path + ".last_hash.json"
    if not os.path.exists(last_hash_path):
        return ""
    try:
        with open(last_hash_path) as f:
            return _json.load(f).get("last_hash", "")
    except (_json.JSONDecodeError, OSError, KeyError):
        return ""


def _current_archives_list(ledger_path):
    """Lista sorted de archivos ``.gz`` en ``<ledger_dir>/archive/`` (o [])."""
    archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")
    if not os.path.isdir(archive_dir):
        return []
    return sorted(f for f in os.listdir(archive_dir) if f.endswith(".gz"))


def _corrupt_dag_hash(dag_path):
    """Corrompe el ``dag_hash`` del DAG cache en disco (simula corrupción).

    Lee el JSON, altera un byte del hash, y lo reescribe. El ``read_dag``
    debe detectar el mismatch y retornar ``None``.
    """
    with open(dag_path, "r") as f:
        raw = f.read()
    loaded = json.loads(raw)
    # Alterar el hash: cambiar el primer char (si es 'a' → 'b', sino 'a').
    stored = loaded.get("dag_hash", "")
    if stored and stored[0] == "a":
        loaded["dag_hash"] = "b" + stored[1:]
    else:
        loaded["dag_hash"] = "a" + stored[1:]
    with open(dag_path, "w") as f:
        f.write(json.dumps(loaded))


def _make_ledger_with_chain(tmp_path, n_events, start_seq=0):
    """Construye un ledger con ``n_events`` eventos encadenados linealmente.

    Cada evento ``i`` escribe ``file_{i}.py`` y lee ``file_{i-1}.py`` (salvo
    el primero, que solo escribe). Los eventos se encadenan vía
    ``parent_event_id``. Retorna ``(ledger_path, writer, [event_ids])``.

    Usa el helper lightweight ``_log_event`` (sin snapshots) — apropiado
    para tests de ``trace_downstream`` y para tests de ``trace_upstream``
    donde se stubbea ``attribute_line``.
    """
    ledger, writer = _make_ledger(tmp_path)
    ids = []
    parent = None
    for i in range(n_events):
        writes = [f"file_{start_seq + i}.py"]
        reads = [f"file_{start_seq + i - 1}.py"] if i > 0 else None
        eid = _log_event(
            writer, writes=writes, reads=reads, parent_event_id=parent,
        )
        ids.append(eid)
        parent = eid
    return ledger, writer, ids


# ---------------------------------------------------------------------------
# 1. trace_upstream usa DAG cache cuando fresh
# ---------------------------------------------------------------------------

def test_trace_upstream_uses_dag_cache_when_fresh(tmp_path):
    """DAG cache fresh → trace_upstream produce el mismo resultado que sin
    cache (el cache acelera pero no cambia la semántica).

    Setup: ledger con 150 eventos encadenados (supera el umbral default
    dag_cache_min_events=100). Se construye y escribe el DAG cache fresh.
    Se stubbea ``attribute_line`` para que trace_upstream use el HEAD como
    writer (los eventos no tienen snapshots). Se compara el resultado con
    y sin cache (borrando el cache entre runs).
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    head_id = ids[-1]

    # Stub attribute_line: retorna el HEAD event como writer (sin snapshots).
    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        # Run SIN cache (cache frío): resultado de referencia.
        result_no_cache = trace_upstream(f"file_{149}.py", 1, ledger)
        cone_no_cache = set(result_no_cache["cone"].keys())

        # Construir y escribir el DAG cache fresh.
        _build_and_write_dag(ledger)

        # Run CON cache fresh.
        result_with_cache = trace_upstream(f"file_{149}.py", 1, ledger)
        cone_with_cache = set(result_with_cache["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    # Los resultados deben ser idénticos (el cache no cambia semántica).
    assert cone_with_cache == cone_no_cache, (
        f"trace_upstream con DAG cache fresh debe producir el mismo cone "
        f"que sin cache. Sin cache: {cone_no_cache}. Con cache: "
        f"{cone_with_cache}."
    )
    assert result_with_cache["depth"] == result_no_cache["depth"], (
        f"depth debe ser igual: sin cache {result_no_cache['depth']}, "
        f"con cache {result_with_cache['depth']}."
    )
    # Sanity: el cone incluye el writer (HEAD).
    assert head_id in cone_with_cache


# ---------------------------------------------------------------------------
# 2. trace_upstream cae a full-scan cuando DAG stale
# ---------------------------------------------------------------------------

def test_trace_upstream_falls_back_when_dag_stale(tmp_path):
    """DAG stale (ledger tiene eventos nuevos no cacheados) → trace_upstream
    cae a full-scan on-the-fly. El resultado debe ser correcto (igual que
    sin cache, incluyendo los eventos nuevos).

    Setup: construir ledger con 150 eventos + DAG cache fresh. Luego
    agregar 50 eventos nuevos al ledger (sin actualizar el cache). El DAG
    queda stale. trace_upstream debe detectar staleness y caer a full-scan,
    produciendo el resultado correcto (con los 200 eventos).
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)

    # Construir DAG cache fresh (cubre los 150 eventos).
    _build_and_write_dag(ledger)

    # Agregar 50 eventos nuevos al ledger (el DAG queda stale).
    parent = ids[-1]
    new_ids = []
    for i in range(150, 200):
        eid = _log_event(
            writer,
            writes=[f"file_{i}.py"],
            reads=[f"file_{i-1}.py"],
            parent_event_id=parent,
        )
        new_ids.append(eid)
        parent = eid
    head_id = new_ids[-1]

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        # trace_upstream con DAG stale → debe caer a full-scan.
        result = trace_upstream(f"file_{199}.py", 1, ledger)
        cone_ids = set(result["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    # El resultado debe incluir eventos nuevos (no cacheados).
    # Si trace_upstream usara el cache stale, los eventos 150-199 no
    # estarían en writer_history y el cone sería incorrecto.
    assert head_id in cone_ids, (
        f"HEAD (evento nuevo) debe estar en el cone. cone={cone_ids}"
    )
    # Sanity: el cone tiene eventos (no está vacío).
    assert len(cone_ids) >= 2, (
        f"cone debe tener >= 2 eventos (chain de 200), got {len(cone_ids)}"
    )


# ---------------------------------------------------------------------------
# 3. trace_upstream cae a full-scan cuando DAG corrupto
# ---------------------------------------------------------------------------

def test_trace_upstream_falls_back_when_dag_corrupt(tmp_path):
    """DAG corrupto (hash mismatch) → trace_upstream cae a full-scan.

    Setup: ledger con 150 eventos + DAG cache escrito. Se corrompe el
    ``dag_hash`` del cache. trace_upstream debe detectar la corrupción
    (vía ``read_dag`` que retorna None) y caer a full-scan, produciendo
    el resultado correcto.
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    head_id = ids[-1]

    # Construir DAG cache fresh.
    dag_path = _build_and_write_dag(ledger)
    # Corromper el hash.
    _corrupt_dag_hash(dag_path)

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        # trace_upstream con DAG corrupto → debe caer a full-scan.
        result = trace_upstream(f"file_{149}.py", 1, ledger)
        cone_ids = set(result["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    # El resultado debe ser correcto (full-scan), incluyendo el writer.
    assert head_id in cone_ids, (
        f"HEAD debe estar en el cone incluso con DAG corrupto. "
        f"cone={cone_ids}"
    )
    assert len(cone_ids) >= 2, (
        f"cone debe tener >= 2 eventos, got {len(cone_ids)}"
    )


# ---------------------------------------------------------------------------
# 4. trace_upstream no usa cache below min_events threshold
# ---------------------------------------------------------------------------

def test_trace_upstream_skips_cache_below_min_events(tmp_path, monkeypatch):
    """``dag_cache_min_events=100``, ledger con 50 eventos → trace_upstream
    no usa el cache (no vale la pena) y cae a full-scan.

    Verificación: se escribe un DAG cache fresh, pero como el ledger tiene
    solo 50 eventos (< 100), trace_upstream debe ignorar el cache y usar
    full-scan. El resultado sigue siendo correcto.

    Para detectar que el cache NO se usó, espiamos ``read_dag`` — si se
    llama, significa que el umbral se respetó mal. Si no se llama, el
    umbral cortó antes del intento de lectura.
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader
    import causadb._dag_cache as dag_cache_mod

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=50)
    head_id = ids[-1]

    # Construir DAG cache fresh (aunque el ledger sea chico).
    _build_and_write_dag(ledger)

    # Espiar read_dag: si se llama, el umbral no cortó.
    read_dag_calls = []
    original_read_dag = dag_cache_mod.read_dag

    def _spying_read_dag(path):
        read_dag_calls.append(path)
        return original_read_dag(path)

    monkeypatch.setattr(dag_cache_mod, "read_dag", _spying_read_dag)

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        result = trace_upstream(f"file_{49}.py", 1, ledger)
        cone_ids = set(result["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    # El umbral (100) debe cortar antes de intentar leer el cache.
    # 50 eventos < 100 → read_dag NO debe ser llamado.
    assert read_dag_calls == [], (
        f"read_dag no debe ser llamado cuando el ledger tiene < "
        f"dag_cache_min_events eventos. calls={read_dag_calls}"
    )
    # El resultado sigue siendo correcto (full-scan).
    assert head_id in cone_ids
    assert len(cone_ids) >= 2


# ---------------------------------------------------------------------------
# 5. trace_downstream usa DAG cache cuando fresh
# ---------------------------------------------------------------------------

def test_trace_downstream_uses_dag_cache_when_fresh(tmp_path):
    """DAG cache fresh → trace_downstream produce el mismo resultado que sin
    cache (el cache acelera el lookup de writes pero no cambia semántica).

    Setup: ledger con 150 eventos encadenados. Se construye y escribe el
    DAG cache fresh. Se compara trace_downstream del primer evento con y
    sin cache (borrando el cache entre runs).
    """
    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    source_id = ids[0]  # primer evento (escribe file_0.py, nadie lee file_0
    # salvo el evento 1, que escribe file_1.py que lee el evento 2, etc.)

    # Run SIN cache (cache frío): resultado de referencia.
    result_no_cache = trace_downstream(source_id, ledger)
    tainted_no_cache = {e["event_id"] for e in result_no_cache}

    # Construir y escribir el DAG cache fresh.
    _build_and_write_dag(ledger)

    # Run CON cache fresh.
    result_with_cache = trace_downstream(source_id, ledger)
    tainted_with_cache = {e["event_id"] for e in result_with_cache}

    # Los resultados deben ser idénticos.
    assert tainted_with_cache == tainted_no_cache, (
        f"trace_downstream con DAG cache fresh debe producir el mismo "
        f"tainted set que sin cache. Sin cache: {tainted_no_cache}. "
        f"Con cache: {tainted_with_cache}."
    )
    # Sanity: el tainted set no está vacío (el source escribe file_0.py
    # que el evento 1 lee, y transitivamente todos los demás).
    assert len(tainted_with_cache) >= 1, (
        f"tainted set debe tener >= 1 evento (transitive taint), got "
        f"{tainted_with_cache}"
    )


# ---------------------------------------------------------------------------
# 6. trace_downstream cae a full-scan cuando DAG stale
# ---------------------------------------------------------------------------

def test_trace_downstream_falls_back_when_dag_stale(tmp_path):
    """DAG stale (ledger tiene eventos nuevos) → trace_downstream cae a
    full-scan. El resultado debe ser correcto (incluyendo eventos nuevos).

    Setup: ledger con 150 eventos + DAG cache fresh. Agregar 50 eventos
    nuevos. trace_downstream del primer evento debe incluir los eventos
    nuevos en el tainted set (transitive taint a través de la cadena).
    """
    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    source_id = ids[0]

    # Construir DAG cache fresh (cubre los 150 eventos).
    _build_and_write_dag(ledger)

    # Agregar 50 eventos nuevos (DAG queda stale).
    parent = ids[-1]
    new_ids = []
    for i in range(150, 200):
        eid = _log_event(
            writer,
            writes=[f"file_{i}.py"],
            reads=[f"file_{i-1}.py"],
            parent_event_id=parent,
        )
        new_ids.append(eid)
        parent = eid

    # trace_downstream con DAG stale → debe caer a full-scan.
    result = trace_downstream(source_id, ledger)
    tainted_ids = {e["event_id"] for e in result}

    # Los eventos nuevos (150-199) deben estar en el tainted set
    # (transitive taint: source escribe file_0 → ev1 lee file_0 y escribe
    # file_1 → ev2 lee file_1 → ... → ev199 lee file_198).
    new_tainted = tainted_ids & set(new_ids)
    assert len(new_tainted) > 0, (
        f"eventos nuevos (no cacheados) deben estar en tainted set si "
        f"hay transitive taint. new_tainted={new_tainted}, "
        f"all_new={set(new_ids)}"
    )


# ---------------------------------------------------------------------------
# 7. Anti-teatro: trace usa stale DAG → detecta eventos faltantes
# ---------------------------------------------------------------------------

def test_anti_teatro_trace_uses_stale_dag(tmp_path, monkeypatch):
    """Anti-teatro (Artículo IX): DAG stale con eventos nuevos NO incluidos
    en el cache → trace_upstream debe usar full-scan (no cache stale).

    Si trace_upstream usara el DAG stale sin verificar ``is_dag_stale``,
    los eventos nuevos no estarían en ``writer_history`` y el cone sería
    incorrecto (faltan eventos nuevos como nodos upstream). Este test
    fuerza el uso del cache stale (stub de ``is_dag_stale`` que retorna
    False) y verifica que el cone es INCORRECTO (faltan eventos nuevos
    upstream). Luego verifica que sin el stub, el cone es CORRECTO.

    Esto prueba que la verificación ``is_dag_stale`` realmente se ejecuta
    en el path de trace_upstream — sin esa verificación, el cache stale
    se usaría y el resultado sería incorrecto.

    Setup:
      - Ledger con 150 eventos + DAG cache fresh (cubre 150).
      - Agregar 50 eventos nuevos (150-199). DAG queda stale.
      - Writer = HEAD (evento 199, stubbeado via attribute_line).
      - Caso A (real): trace_upstream verifica is_dag_stale → full-scan
        → cone incluye eventos nuevos como upstream (199 lee file_198,
        198 escribe file_198, etc.).
      - Caso B (forzado): stub is_dag_stale → False → cache stale se
        usa → writer_history del cache NO tiene file_198.py (evento 198
        es nuevo) → BFS no encuentra upstream → cone = {writer} solo.
    """
    import causadb._causal_attrib as attrib_mod
    import causadb._dag_cache as dag_cache_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)

    # Construir DAG cache fresh (cubre los 150 eventos).
    _build_and_write_dag(ledger)

    # Agregar 50 eventos nuevos (DAG queda stale).
    parent = ids[-1]
    new_ids = []
    for i in range(150, 200):
        eid = _log_event(
            writer,
            writes=[f"file_{i}.py"],
            reads=[f"file_{i-1}.py"],
            parent_event_id=parent,
        )
        new_ids.append(eid)
        parent = eid
    head_id = new_ids[-1]

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line

    # --- Caso B (PRIMERO): forzar uso del cache stale (stub is_dag_stale → False) ---
    # Si trace_upstream usara el cache stale, writer_history del cache
    # no tiene entries para file_198.py (evento 198 es nuevo, no cacheado)
    # → el BFS del HEAD no encuentra upstream → cone = {writer} solo.
    # NOTA: este caso debe correr ANTES que el Caso A — el Caso A (real)
    # detecta la staleness, hace REBUILD y ESCRIBE el cache con los 200
    # eventos, destruyendo el estado stale que este caso necesita.
    attrib_mod.attribute_line = _stub_attribute_line
    original_is_stale = dag_cache_mod.is_dag_stale
    monkeypatch.setattr(dag_cache_mod, "is_dag_stale", lambda dag, lp: False)
    try:
        result_stale_cache = trace_upstream(f"file_{199}.py", 1, ledger)
        cone_stale_cache = set(result_stale_cache["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr
        monkeypatch.setattr(dag_cache_mod, "is_dag_stale", original_is_stale)

    # Con el cache stale forzado, el HEAD (writer) sigue en el cone
    # (siempre se añade como nodo inicial), pero los eventos nuevos
    # upstream NO deben estar — porque writer_history del cache stale
    # no los tiene.
    assert head_id in cone_stale_cache, (
        f"HEAD (writer) siempre debe estar en el cone, incluso con "
        f"cache stale. cone={cone_stale_cache}"
    )
    # Excluir el HEAD de new_in_stale: el HEAD es nuevo (es new_ids[-1])
    # pero siempre está en el cone como writer, no como upstream. Lo que
    # queremos verificar es que los eventos nuevos NO aparecen como
    # UPSTREAM (vía writer_history del cache stale).
    new_upstream_in_stale = (cone_stale_cache & set(new_ids)) - {head_id}
    assert new_upstream_in_stale == set(), (
        f"con cache stale forzado, los eventos nuevos (no cacheados) "
        f"NO deben aparecer como upstream — si aparecen, el cache stale "
        f"no se está usando y el anti-teatro check es vacío. "
        f"new_upstream_in_stale={new_upstream_in_stale}"
    )

    # --- Caso A (DESPUÉS): trace_upstream real (verifica is_dag_stale → full-scan) ---
    # El resultado debe ser CORRECTO: el cone incluye eventos nuevos
    # como nodos upstream (199 lee file_198 → 198 es upstream, etc.).
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        result_correct = trace_upstream(f"file_{199}.py", 1, ledger)
        cone_correct = set(result_correct["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    assert head_id in cone_correct, (
        f"trace_upstream real debe incluir HEAD (writer) en el cone. "
        f"cone={cone_correct}"
    )
    # El cone correcto debe tener múltiples eventos (la cadena upstream
    # de 199 incluye 198, 197, ..., 0).
    assert len(cone_correct) >= 2, (
        f"cone correcto (full-scan) debe tener >= 2 eventos (cadena "
        f"upstream), got {len(cone_correct)}: {cone_correct}"
    )
    # Eventos nuevos deben aparecer como upstream (al menos el 198,
    # que escribe file_198.py que 199 lee).
    new_in_correct = cone_correct & set(new_ids)
    assert len(new_in_correct) >= 1, (
        f"al menos un evento nuevo debe estar en el cone correcto como "
        f"upstream. new_in_correct={new_in_correct}"
    )

    # El cone correcto (caso A) tiene MÁS eventos que el cone stale
    # forzado (caso B), porque el caso A encuentra los upstream nuevos
    # vía full-scan mientras el caso B no los encuentra vía cache stale.
    assert len(cone_correct) > len(cone_stale_cache), (
        f"el cone correcto (full-scan) debe tener MÁS eventos que el "
        f"cone con cache stale forzado. correct={len(cone_correct)} "
        f"({cone_correct}), stale={len(cone_stale_cache)} "
        f"({cone_stale_cache})"
    )


# ===========================================================================
# BIT-CHR.117 — Normalización de paths en el cono causal
# ---------------------------------------------------------------------------
# BIT-CHR.110 TD-#3d: el ledger real mezcla estilos
# (``causadb/causadb/X`` vs ``causadb/X``). La normalización debe hacer
# que trace_downstream/upstream conecten eventos que escriben/leen el
# mismo archivo con estilos distintos.
# ===========================================================================

def test_anti_teatro_normalization_connects_downstream(tmp_path):
    """Writes ``causadb/causadb/main.py`` + reads ``causadb/main.py`` en
    eventos distintos → trace_downstream los conecta (misma clave).

    Sin normalización, el evento B (reads ``causadb/main.py``) NO
    intersectaría con los writes de A (``causadb/causadb/main.py``) y el
    cono quedaría vacío — el test fallaría.
    """
    ledger, writer = _make_ledger(tmp_path)

    a_id = _log_event(writer, writes=["causadb/causadb/main.py"])
    b_id = _log_event(writer, reads=["causadb/main.py"])

    result = trace_downstream(a_id, ledger)
    tainted_ids = {e["event_id"] for e in result}

    assert b_id in tainted_ids, (
        f"B lee causadb/main.py que A escribió como causadb/causadb/main.py "
        f"— la normalización debe conectarlos. tainted={tainted_ids}"
    )


def test_anti_teatro_normalization_connects_upstream(tmp_path):
    """Writes ``causadb/causadb/spec.md`` + reads ``causadb/spec.md`` →
    trace_upstream conecta el writer previo (misma clave normalizada).

    Setup (sin snapshots — se stubbea attribute_line):
      - ev_root: escribe ``causadb/causadb/spec.md``.
      - W: lee ``causadb/spec.md``, escribe ``main.py``.
    trace_upstream(main.py, 1) debe incluir ev_root en el cone.
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer = _make_ledger(tmp_path)

    root_id = _log_event(writer, writes=["causadb/causadb/spec.md"])
    w_id = _log_event(
        writer,
        reads=["causadb/spec.md"],
        writes=["main.py"],
        parent_event_id=root_id,
    )

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        result = trace_upstream("main.py", 1, ledger)
        cone_ids = set(result["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    assert w_id in cone_ids, "W (writer) debe estar en el cone"
    assert root_id in cone_ids, (
        f"ev_root (escribió causadb/causadb/spec.md que W leyó como "
        f"causadb/spec.md) debe estar en el cone tras normalización. "
        f"cone={cone_ids}"
    )


# ===========================================================================
# BIT-CHR.117 — trace desde el DAG cache SIN materializar el ledger
# ---------------------------------------------------------------------------
# Anti-teatro: con el cache warm, trace_upstream/trace_downstream NO
# deben llamar a ``_iter_events_from`` (materialización completa). Si lo
# hacen, el monkeypatch levanta AssertionError y el test falla.
# ===========================================================================

def test_anti_teatro_trace_upstream_from_cache_without_materializing(tmp_path, monkeypatch):
    """Cache warm → trace_upstream funciona sin materializar el ledger.

    (1) warm-build el cache; (2) monkeypatch ``_iter_events_from`` para
    que levante AssertionError si se llama; (3) trace_upstream debe
    funcionar y dar el resultado correcto.
    """
    import causadb._causal_attrib as attrib_mod
    import causadb._causal_cone as cone_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    head_id = ids[-1]

    # Warm-build el cache.
    _build_and_write_dag(ledger)

    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    def _no_materialize(ledger_path):
        raise AssertionError(
            "_iter_events_from NO debe llamarse con cache warm — "
            "trace_upstream debe operar desde el DAG cache"
        )

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    monkeypatch.setattr(cone_mod, "_iter_events_from", _no_materialize)
    try:
        result = trace_upstream(f"file_{149}.py", 1, ledger)
        cone_ids = set(result["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    assert head_id in cone_ids, (
        f"HEAD (writer) debe estar en el cone desde el cache. cone={cone_ids}"
    )
    assert len(cone_ids) >= 2, (
        f"el cone desde cache debe tener >= 2 eventos (cadena upstream), "
        f"got {len(cone_ids)}"
    )


def test_anti_teatro_trace_downstream_from_cache_without_materializing(tmp_path, monkeypatch):
    """Cache warm → trace_downstream funciona sin materializar el ledger."""
    import causadb._causal_cone as cone_mod

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=150)
    source_id = ids[0]

    # Warm-build el cache.
    _build_and_write_dag(ledger)

    def _no_materialize(ledger_path):
        raise AssertionError(
            "_iter_events_from NO debe llamarse con cache warm — "
            "trace_downstream debe operar desde el DAG cache"
        )

    monkeypatch.setattr(cone_mod, "_iter_events_from", _no_materialize)

    result = trace_downstream(source_id, ledger)
    tainted_ids = {e["event_id"] for e in result}

    assert len(tainted_ids) >= 1, (
        f"trace_downstream desde cache debe taintar >= 1 evento "
        f"(transitive taint), got {tainted_ids}"
    )
    # Los entries deben tener event_type/timestamp (contrato de los
    # consumidores — mcp/_tools.py devuelve el dict tal cual).
    for entry in result:
        assert "event_type" in entry, "entry debe tener event_type"
        assert "timestamp" in entry, "entry debe tener timestamp"
        assert "tainted_by" in entry, "entry debe tener tainted_by"


# ===========================================================================
# BIT-CHR.117 — Incremental: cache + eventos nuevos → update_dag
# ===========================================================================

def test_trace_sees_new_events_after_incremental_update(tmp_path):
    """Cache construido con 2 eventos; append 1 evento nuevo (con writes);
    trace_downstream/upstream lo ve (cache refrescado por update_dag).

    Setup:
      - ev0: escribe file_0.py.
      - ev1: lee file_0.py, escribe file_1.py.
      - Cache warm con ev0+ev1.
      - ev2: lee file_1.py, escribe file_2.py (nuevo, NO en cache).
    trace_downstream(ev0) debe incluir ev2 (transitive taint).
    trace_upstream(file_2.py) debe incluir ev1 y ev0.
    """
    import causadb._causal_attrib as attrib_mod
    from causadb._ledger_reader import LedgerReader

    ledger, writer = _make_ledger(tmp_path)
    ev0 = _log_event(writer, writes=["file_0.py"])
    ev1 = _log_event(writer, reads=["file_0.py"], writes=["file_1.py"], parent_event_id=ev0)

    # Warm-build el cache con los 2 eventos.
    _build_and_write_dag(ledger)

    # Append 1 evento nuevo (ev2).
    ev2 = _log_event(writer, reads=["file_1.py"], writes=["file_2.py"], parent_event_id=ev1)

    # trace_downstream(ev0) debe ver ev2 (vía update_dag incremental).
    result_down = trace_downstream(ev0, ledger)
    tainted_ids = {e["event_id"] for e in result_down}
    assert ev1 in tainted_ids, "ev1 debe estar tainted (lee file_0.py)"
    assert ev2 in tainted_ids, (
        f"ev2 (nuevo, no cacheado) debe estar tainted vía update_dag "
        f"incremental. tainted={tainted_ids}"
    )

    # trace_upstream(file_2.py) debe ver ev1 y ev0 (vía update_dag).
    def _stub_attribute_line(file_path, line_number, ledger_path):
        reader = LedgerReader(ledger_path)
        events = list(reader.read_all())
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    original_attr = attrib_mod.attribute_line
    attrib_mod.attribute_line = _stub_attribute_line
    try:
        result_up = trace_upstream("file_2.py", 1, ledger)
        cone_ids = set(result_up["cone"].keys())
    finally:
        attrib_mod.attribute_line = original_attr

    assert ev2 in cone_ids, "ev2 (writer) debe estar en el cone"
    assert ev1 in cone_ids, (
        f"ev1 (escribió file_1.py que ev2 lee) debe estar en el cone "
        f"tras update_dag. cone={cone_ids}"
    )
    assert ev0 in cone_ids, (
        f"ev0 (escribió file_0.py que ev1 lee) debe estar en el cone "
        f"tras update_dag. cone={cone_ids}"
    )


# ===========================================================================
# BIT-CHR.117 — Guard de truncamiento (ledger truncado → rebuild)
# ===========================================================================

def test_is_dag_stale_true_when_ledger_truncated(tmp_path):
    """Cache construido, ledger truncado a la mitad → ``is_dag_stale`` True.

    El guard de tamaño (``size < dag.last_offset``) debe detectar el
    truncamiento aunque los sequence_number coincidan (por ejemplo tras
    un restore desde backup).
    """
    from causadb._dag_cache import is_dag_stale

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=10)

    # Warm-build el cache (last_offset = fin del ledger).
    _build_and_write_dag(ledger)

    # Truncar el ledger a la mitad.
    with open(ledger, "rb") as f:
        data = f.read()
    half = len(data) // 2
    with open(ledger, "wb") as f:
        f.write(data[:half])

    dag_path = _dag_path_for(ledger)
    from causadb._dag_cache import read_dag
    dag = read_dag(dag_path)
    assert dag is not None, "el cache debe leerse antes del truncamiento"

    assert is_dag_stale(dag, ledger) is True, (
        "ledger truncado (size < last_offset) debe marcar el cache stale"
    )


def test_load_or_build_dag_rebuilds_when_ledger_truncated(tmp_path, monkeypatch):
    """Ledger truncado → ``_load_or_build_dag`` hace REBUILD (no update).

    Verificación: monkeypatch ``_iter_events_from`` con un contador. Si
    el path es rebuild, se llama 1 vez; si fuera update (tail-read), no
    se llamaría (o se llamaría distinto). El resultado debe ser correcto.
    """
    import causadb._causal_cone as cone_mod
    from causadb._causal_cone import _load_or_build_dag

    ledger, writer, ids = _make_ledger_with_chain(tmp_path, n_events=10)

    # Warm-build el cache.
    _build_and_write_dag(ledger)

    # Truncar el ledger a la mitad.
    with open(ledger, "rb") as f:
        data = f.read()
    half = len(data) // 2
    with open(ledger, "wb") as f:
        f.write(data[:half])

    # Contar llamadas a _iter_events_from (rebuild materializa; update no).
    calls = []

    def _counting_iter(ledger_path):
        calls.append(ledger_path)
        return cone_mod._iter_events_from.__wrapped__(ledger_path) if hasattr(
            cone_mod._iter_events_from, "__wrapped__"
        ) else _real_iter(ledger_path)

    # Guardar la referencia real antes de monkeypatch.
    _real_iter = cone_mod._iter_events_from
    monkeypatch.setattr(cone_mod, "_iter_events_from", _counting_iter)

    dag = _load_or_build_dag(ledger, min_events=1)

    assert dag is not None, "_load_or_build_dag debe retornar un DAG"
    assert len(calls) >= 1, (
        f"ledger truncado debe forzar rebuild (materialización), "
        f"calls={len(calls)}"
    )
    # El DAG rebuild debe cubrir los eventos que quedaron en el ledger.
    from causadb._dag_cache import get_last_ledger_seq
    assert dag["last_seq"] == get_last_ledger_seq(ledger), (
        f"el DAG rebuild debe cubrir el último seq del ledger truncado, "
        f"got {dag['last_seq']}"
    )
