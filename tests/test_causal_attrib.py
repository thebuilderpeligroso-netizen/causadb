"""Tests for F.12.2 — `causadb why file:line` (causal line attribution).

Artículo III: Test-first. Tests written BEFORE implementation.
Artículo IX: Anti-teatro — every test has discriminatory power.

The 7 tests below mirror the spec in
``CAUSADB_ROADMAP_FASE12_PLAN.md`` (F.12.2 section, lines 243-249).
"""
import json
import os
from types import MappingProxyType

import pytest
import anyio

from causadb._blob_store import BlobStore
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb._config import CausaDBConfig
from causadb._snapshot import WorkspaceSnapshot
from tests.helpers._mcp_call import _call_tool


# ---------------------------------------------------------------------------
# Helpers — build a workspace + ledger with pre/post snapshots
# ---------------------------------------------------------------------------

def _write_file(path, content):
    with open(path, "w") as f:
        f.write(content)


def _remove_if_exists(path):
    if os.path.exists(path):
        os.remove(path)


def _make_workspace(tmp_path, name="ws"):
    """Create a workspace dir + BlobStore + CausaDBConfig wired for snapshots.

    The BlobStore is rooted at ``dirname(ledger)/blobs`` — the SAME default
    that ``causadb._causal_attrib._blob_store_for`` derives from the ledger
    path. This keeps the plan signature ``attribute_line(file, line, ledger)``
    intact (no extra blob_store_path parameter needed).
    """
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


def _store_snapshot(snapshot, store, root_dir):
    return WorkspaceSnapshot.store(snapshot, store, root_dir=root_dir)


def _log_event(
    writer, store, ws, rel_path,
    pre_present: bool, pre_content: str,
    post_present: bool, post_content: str,
    source="opencode:agent1",
    source_type="agent",
    prompt=None, reasoning=None,
    parent_event_id=None,
    event_type=EventType.FILE_MODIFIED,
    ctx_id="ctx",
):
    """Build a deterministic event with explicit pre/post snapshots.

    *pre_present* / *post_present* control whether the file exists in the
    pre / post snapshot. When False, the file is removed before the snapshot
    is taken (so it is genuinely absent, not empty).

    Returns the CanonicalEvent that was appended.
    """
    target = os.path.join(ws, rel_path)

    # --- pre snapshot ---
    if pre_present:
        _write_file(target, pre_content)
    else:
        _remove_if_exists(target)
    pre_snap = WorkspaceSnapshot.take(ws)
    pre_hash = _store_snapshot(pre_snap, store, ws)

    # --- post snapshot ---
    if post_present:
        _write_file(target, post_content)
    else:
        _remove_if_exists(target)
    post_snap = WorkspaceSnapshot.take(ws, prev_snapshot=pre_snap)
    post_hash = _store_snapshot(post_snap, store, ws)

    payload = {
        "action": "modified",
        "path": target,
        "writes": [rel_path],
        "pre_snapshot": pre_hash,
        "post_snapshot": post_hash,
    }
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
    return event


# ---------------------------------------------------------------------------
# 1. test_why_finds_introducing_event
# ---------------------------------------------------------------------------

def test_why_finds_introducing_event(tmp_path):
    """Event 1 creates main.py with "x = 1\\n". Event 2 appends "y = 2\\n".
    `attribute_line("main.py", 2)` must return event 2 (it introduced line 2).
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Event 1: main.py absent in pre → present in post with "x = 1\n" (line 1).
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="create main", reasoning="initial scaffold",
    )

    # Event 2: main.py present in pre ("x = 1\n") → present in post with
    # "x = 1\ny = 2\n" (line 2 is new).
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        prompt="add y", reasoning="need y for calc",
        parent_event_id=ev1.event_id,
    )

    # Line 2 ("y = 2") was introduced by ev2.
    result = attribute_line("main.py", 2, ledger)
    assert result is not None, "expected an introducer event for line 2"
    assert result["event_id"] == ev2.event_id, (
        f"expected ev2 ({ev2.event_id}), got {result.get('event_id')}"
    )


# ---------------------------------------------------------------------------
# 2. test_why_returns_none_if_line_never_introduced
# ---------------------------------------------------------------------------

def test_why_returns_none_if_line_never_introduced(tmp_path):
    """Line 1 ("x = 1") existed since the root event (ev1 introduced it).
    No later event introduced it — so the introducer is the root event (ev1).

    Per plan: "línea que existía desde el principio (root event) → retorna
    root event como introducer". We return the root event (not None) because
    the line WAS introduced — by the root. The "none" in the test name refers
    to "no LATER event introduced it"; the root is the introducer.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Event 1 (root): introduce main.py with "x = 1\n" (line 1).
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="create", reasoning="root",
    )

    # Event 2 modifies a DIFFERENT file (utils.py), not main.py.
    ev2 = _log_event(
        writer, store, ws, "utils.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="u = 1\n",
        prompt="add utils", reasoning="helper",
        parent_event_id=ev1.event_id,
    )

    # Line 1 of main.py was introduced by ev1 (the root). No later event
    # touched main.py, so the introducer is the root event ev1.
    result = attribute_line("main.py", 1, ledger)
    assert result is not None, (
        "line 1 was introduced by the root event — root is the introducer"
    )
    assert result["event_id"] == ev1.event_id, (
        f"expected root event ev1, got {result.get('event_id')}"
    )


# ---------------------------------------------------------------------------
# 3. test_why_returns_prompt_and_reasoning
# ---------------------------------------------------------------------------

def test_why_returns_prompt_and_reasoning(tmp_path):
    """The introducer event result must include prompt and reasoning fields."""
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="add the x variable",
        reasoning="we need x for the calculation later",
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None
    assert result.get("prompt") == "add the x variable", (
        f"expected prompt, got {result.get('prompt')!r}"
    )
    assert result.get("reasoning") == "we need x for the calculation later", (
        f"expected reasoning, got {result.get('reasoning')!r}"
    )


# ---------------------------------------------------------------------------
# 4. test_why_works_with_manual_events
# ---------------------------------------------------------------------------

def test_why_works_with_manual_events(tmp_path):
    """An event logged by hand (no vigilante) with writes + snapshots works."""
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    manual_event = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="manual_line = 42\n",
        source="human:operator",
        source_type="human",
        prompt="manual edit",
        reasoning="operator hand-edit",
        ctx_id="manual-ctx",
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None, "manual event must be found"
    assert result["event_id"] == manual_event.event_id
    assert result["source"] == "human:operator"


# ---------------------------------------------------------------------------
# 5. test_why_file_not_in_ledger_raises
# ---------------------------------------------------------------------------

def test_why_file_not_in_ledger_raises(tmp_path):
    """File never touched in the ledger → ValueError with informative message."""
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Log an event that touches main.py only.
    _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
    )

    # never_touched.py was never in any snapshot.
    with pytest.raises(ValueError) as exc_info:
        attribute_line("never_touched.py", 1, ledger)
    assert "never_touched.py" in str(exc_info.value), (
        f"error message must mention the file, got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# 6. test_anti_teatro_why_skips_walk
# ---------------------------------------------------------------------------

def test_anti_teatro_why_skips_walk(tmp_path):
    """Mutate `attribute_line` to skip the reverse snapshot scan (return
    HEAD immediately) → test_why_finds_introducing_event's contract breaks
    (wrong event). Then RESTORE in try/finally.

    Anti-teatro (Article IX): a stub that returns the HEAD event without
    scanning the full snapshot history would return ev2 (HEAD) for line 1
    too, which is WRONG (line 1 was introduced by ev1, not ev2). This
    test proves the reverse-order snapshot iteration (which replaced the
    old parent-chain walk — BIT-CHR.110 TD-#3a) is actually exercised.
    """
    from causadb._causal_attrib import attribute_line
    import causadb._causal_attrib as attrib_mod

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Event 1: introduce line 1 ("x = 1").
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    # Event 2: append line 2 ("y = 2"). HEAD is now ev2.
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        prompt="ev2", reasoning="second",
        parent_event_id=ev1.event_id,
    )

    # Sanity: real implementation returns ev1 for line 1.
    real_result = attribute_line("main.py", 1, ledger)
    assert real_result is not None, "real attribute_line must return ev1"
    assert real_result["event_id"] == ev1.event_id, (
        f"real impl must return ev1 for line 1, got {real_result['event_id']}"
    )

    # --- MUTATION: stub attribute_line to skip the walk — return HEAD event ---
    # We build a stub that reads ONLY the last event (HEAD) and returns it,
    # without walking back via parent_event_id. For line 1 this returns ev2
    # (wrong), proving the walk-back is what makes the real impl correct.
    original = attrib_mod.attribute_line

    def _stub_skip_walk(file_path, line_number, ledger_path):
        reader = attrib_mod.LedgerReader(ledger_path)
        events = list(reader.read_all())
        if not events:
            return None
        head = events[-1]
        return {
            "event_id": head.event_id,
            "event_type": head.event_type if isinstance(head.event_type, str) else head.event_type.value,
            "source": head.source,
            "timestamp": head.timestamp,
            "prompt": head.payload.get("prompt"),
            "reasoning": head.payload.get("reasoning"),
            "agent": head.source,
            "parent_event_id": head.parent_event_id,
        }

    attrib_mod.attribute_line = _stub_skip_walk
    try:
        mutated_result = attrib_mod.attribute_line("main.py", 1, ledger)
        # Under the mutation, the stub returns ev2 (HEAD) for line 1 — WRONG.
        assert mutated_result is not None, "stub must return something"
        assert mutated_result["event_id"] == ev2.event_id, (
            "mutated stub must return HEAD (ev2) — otherwise mutation is vacuous"
        )
        # The contract "line 1 → ev1" BREAKS under the mutation.
        assert mutated_result["event_id"] != ev1.event_id, (
            "mutation must break the contract: line 1 should map to ev1, "
            "but the skip-walk stub returned ev2"
        )
    finally:
        # --- RESTORE ---
        attrib_mod.attribute_line = original

    # After restore, the real implementation works again.
    restored_result = attribute_line("main.py", 1, ledger)
    assert restored_result is not None, "restored impl must return ev1"
    assert restored_result["event_id"] == ev1.event_id, (
        f"restored impl must return ev1, got {restored_result['event_id']}"
    )
    assert restored_result == real_result, "restore must be exact"


# ---------------------------------------------------------------------------
# 7. test_mcp_why_tool_works
# ---------------------------------------------------------------------------

def test_mcp_why_tool_works(tmp_path):
    """The MCP tool `why` returns the same result as the nucleus."""
    from causadb.mcp.server import create_server

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="mcp test", reasoning="via mcp",
    )

    server = create_server()

    content_blocks, _ = _call_tool(server, "why", {
        "file_path": "main.py",
        "line_number": 1,
        "ledger_path": ledger,
    })
    text = "".join(getattr(b, "text", str(b)) for b in content_blocks)
    result = json.loads(text)

    assert result["introducer"]["event_id"] == ev.event_id, (
        f"MCP tool must return ev ({ev.event_id}), "
        f"got {result.get('introducer', {}).get('event_id')}"
    )
    assert result["introducer"]["prompt"] == "mcp test"
    assert result["introducer"]["reasoning"] == "via mcp"


# ---------------------------------------------------------------------------
# 8. TD-#3a — why works without parent_event_id links
# ---------------------------------------------------------------------------
# BIT-CHR.110 TD-#3a: en el ledger real el harvester NO setea
# ``parent_event_id`` (None en ~99.99% de los eventos), así que el walk
# por cadena se detiene en HEAD y `why` es inoperante. El algoritmo
# corregido itera TODOS los eventos con post_snapshot en orden de ledger
# inverso (no depende de la cadena). Anti-teatro: ev2 es HEAD y su post
# TAMBIÉN contiene la línea (snapshot completo del workspace) — si el
# test atribuyera a ev2, sería teatro. Debe atribuir a ev1.

def test_why_works_without_parent_links(tmp_path):
    """No parent_event_id set on ANY event — attribute must still walk the
    full ledger (reverse order) and attribute line 1 of main.py to ev1.

    ev2 is HEAD and its post snapshot ALSO contains main.py:1 (full
    workspace snapshot), but ev1 is the true introducer (line absent in
    ev1's pre). Attribution must NOT stop at HEAD just because
    parent_event_id is None.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Event 1: introduce main.py line 1. NO parent (default None).
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    # Event 2: touches utils.py. NO parent (default None). HEAD = ev2.
    ev2 = _log_event(
        writer, store, ws, "utils.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="u = 1\n",
        prompt="ev2", reasoning="second",
    )

    # ev2.post also contains main.py:1 (complete workspace snapshot), but
    # ev2.pre ALSO contains it — ev2 did not introduce the line. The
    # introducer is ev1 (line absent in ev1.pre).
    result = attribute_line("main.py", 1, ledger)
    assert result is not None, (
        "attribute_line must find an introducer even with no parent links"
    )
    assert result["event_id"] == ev1.event_id, (
        f"line 1 of main.py was introduced by ev1, got "
        f"{result.get('event_id')} (ev2 is HEAD but did NOT introduce it)"
    )
    assert result["event_id"] != ev2.event_id, (
        "ev2 (HEAD) must NOT be the introducer: its pre already contains "
        "main.py:1 — attributing to HEAD would be theater"
    )


# ---------------------------------------------------------------------------
# 9. TD-#3c — why falls back to the root of snapshots
# ---------------------------------------------------------------------------
# BIT-CHR.110 TD-#3c: cuando la línea NO tiene una transición (existía
# antes del primer evento del ledger — pre y post la contienen en TODOS
# los eventos), `attribute_line` debe retornar el evento MÁS VIEJO cuyo
# post_snapshot contiene la línea (el root de snapshots: fue quien
# primero capturó la línea en un snapshot). Sin fallback retornaría None
# aunque la línea SÍ existe en snapshots.

def test_why_root_of_snapshots_pre_existing_file(tmp_path):
    """main.py:1 pre-exists BEFORE any event → no event transitions it.
    The root of snapshots (ev1, the oldest event whose post contains the
    line) must be returned, not None.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # File pre-exists before any event is logged.
    _write_file(os.path.join(ws, "main.py"), "x = 1\n")

    # ev1: file present in pre AND post (no transition — it pre-existed).
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="root",
    )

    # ev2: touches a different file.
    ev2 = _log_event(
        writer, store, ws, "utils.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="u = 1\n",
        prompt="ev2", reasoning="second",
    )

    # The line pre-existed in every event's pre and post — no introducer
    # transition. The root of snapshots (ev1) is the answer.
    result = attribute_line("main.py", 1, ledger)
    assert result is not None, (
        "line exists in snapshots — root of snapshots must be returned, "
        "not None"
    )
    assert result["event_id"] == ev1.event_id, (
        f"root of snapshots is ev1 (oldest event whose post contains the "
        f"line), got {result.get('event_id')}"
    )
    assert result["event_id"] != ev2.event_id, (
        "ev2 must NOT be the fallback: ev1 is older and its post also "
        "contains the line"
    )


# ---------------------------------------------------------------------------
# 10. TD-#3d — why normalizes the double "causadb/causadb/" prefix
# ---------------------------------------------------------------------------
# BIT-CHR.110 TD-#3d: en el ledger real los paths de los snapshots tienen
# estilos mixtos para el mismo archivo (`causadb/causadb/_assistant.py`
# vs `causadb/_assistant.py`). La normalización colapsa el doble prefijo
# `causadb/causadb/` → `causadb/` (heurística exacta de BIT-CHR.110).
# NO hace suffix-match (YAGNI): `config.py` NO debe resolver a
# `causadb/config.py`.

def test_why_normalizes_double_causadb_prefix(tmp_path):
    """Snapshot key is ``causadb/causadb/_assistant.py``; querying with
    the single-prefix form ``causadb/_assistant.py`` must resolve to it.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # Create the nested dir so the snapshot key gets the double prefix.
    os.makedirs(os.path.join(ws, "causadb", "causadb"), exist_ok=True)

    # Event 1: introduces causadb/causadb/_assistant.py line 1. The
    # snapshot stores the key with the DOUBLE prefix.
    ev1 = _log_event(
        writer, store, ws, "causadb/causadb/_assistant.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    # Query with the single-prefix form → normalization must resolve.
    result = attribute_line("causadb/_assistant.py", 1, ledger)
    assert result is not None, (
        "normalized query must resolve the double-prefixed snapshot key"
    )
    assert result["event_id"] == ev1.event_id, (
        f"expected ev1 ({ev1.event_id}) for normalized path, "
        f"got {result.get('event_id')}"
    )


def test_why_double_prefix_does_not_false_match(tmp_path):
    """The normalization is NARROW: ``config.py`` must NOT resolve to the
    snapshot key ``causadb/config.py`` (no suffix-match). Querying
    ``config.py`` against a ledger that only touched ``causadb/config.py``
    must keep raising ValueError.
    """
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    os.makedirs(os.path.join(ws, "causadb"), exist_ok=True)

    # Event 1: introduces causadb/config.py (single prefix).
    _log_event(
        writer, store, ws, "causadb/config.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="k = 1\n",
        prompt="ev1", reasoning="first",
    )

    # config.py is NOT causadb/config.py — exact and normalized match both
    # fail, and suffix-match is deliberately NOT performed (YAGNI).
    with pytest.raises(ValueError) as exc_info:
        attribute_line("config.py", 1, ledger)
    assert "config.py" in str(exc_info.value), (
        f"error message must mention the queried file, got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# 11. Index-based attribution (BIT-CHR.115 residual debt #1) — anti-teatro
# ---------------------------------------------------------------------------
# The persistent file index (`causadb._file_index`) replaces the full
# ledger read with blob resolution (~163K events, ~119MB, ~3s on the real
# ledger) by an O(1) lookup + resolving only the candidate events'
# snapshots. These tests prove the index path is actually exercised and
# the legacy full-scan machinery is gone.

def test_why_does_not_full_resolve(tmp_path, monkeypatch):
    """`attribute_line` must NOT call `LedgerReader.read_all` (the full
    read with blob resolution). The index path only uses
    `read_all_entries(resolve_blobs=False)` — a monkeypatched `read_all`
    that raises proves the full-resolve path is never taken."""
    from causadb._causal_attrib import attribute_line
    import causadb._ledger_reader as reader_mod

    def _boom(*args, **kwargs):
        raise AssertionError("read_all no debe llamarse")

    monkeypatch.setattr(reader_mod.LedgerReader, "read_all", _boom)

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None, "index-based attribute_line must find ev1"
    assert result["event_id"] == ev1.event_id, (
        f"expected ev1 ({ev1.event_id}), got {result.get('event_id')}"
    )


def test_why_legacy_gate_scan_removed(tmp_path):
    """The legacy full-scan gate `_file_ever_in_ledger` must be GONE
    (dead code after the index), and attribute_line still returns the
    correct event."""
    import causadb._causal_attrib as m
    from causadb._causal_attrib import attribute_line

    assert not hasattr(m, "_file_ever_in_ledger"), (
        "_file_ever_in_ledger must be removed (dead code after the index)"
    )

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None
    assert result["event_id"] == ev1.event_id, (
        f"expected ev1 ({ev1.event_id}), got {result.get('event_id')}"
    )


def test_why_incremental_sees_new_event(tmp_path):
    """After the index is built, appending a new event must be visible on
    the next query (tail-extend via last_hash + last_offset) — no full
    rebuild needed."""
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # ev1: introduces line 1 of main.py.
    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="first",
    )

    result1 = attribute_line("main.py", 1, ledger)
    assert result1 is not None
    assert result1["event_id"] == ev1.event_id

    # ev2: appends line 2 — the index must be tail-extended.
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\ny = 2\n",
        prompt="ev2", reasoning="second",
        parent_event_id=ev1.event_id,
    )

    result2 = attribute_line("main.py", 2, ledger)
    assert result2 is not None, "line 2 was introduced by ev2"
    assert result2["event_id"] == ev2.event_id, (
        f"index must be tail-extended to see ev2 ({ev2.event_id}), "
        f"got {result2.get('event_id')}"
    )


def test_why_custom_event_type_string(tmp_path):
    """An event with a string `event_type` (registered custom type) must
    be returned WITHOUT AttributeError — proves the getattr fix in
    `_introducer_dict` for dict records (a plain string has no `.value`)."""
    from causadb._causal_attrib import attribute_line
    from causadb._event_registry import EventTypeSpec, register_type

    register_type("CUSTOM_TYPE", EventTypeSpec(required_fields=set()))

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    ev = _log_event(
        writer, store, ws, "main.py",
        pre_present=False, pre_content="",
        post_present=True, post_content="x = 1\n",
        prompt="custom", reasoning="custom type",
        event_type="CUSTOM_TYPE",
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None, "custom-type event must be found"
    assert result["event_id"] == ev.event_id
    assert result["event_type"] == "CUSTOM_TYPE", (
        f"expected 'CUSTOM_TYPE', got {result.get('event_type')!r}"
    )


def test_why_fallback_after_index(tmp_path):
    """Pre-existing file (written before any event): ev1 and ev2 both
    contain the line in pre AND post → no transition → the fallback must
    be the OLDEST event whose post contains the line (ev1), through the
    index path."""
    from causadb._causal_attrib import attribute_line

    ws, store, config, ledger = _make_workspace(tmp_path)
    writer = LedgerWriter(ledger, config=config)

    # File pre-exists before any event is logged.
    _write_file(os.path.join(ws, "main.py"), "x = 1\n")

    ev1 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\n",
        prompt="ev1", reasoning="root",
    )
    ev2 = _log_event(
        writer, store, ws, "main.py",
        pre_present=True, pre_content="x = 1\n",
        post_present=True, post_content="x = 1\n",
        prompt="ev2", reasoning="second",
        parent_event_id=ev1.event_id,
    )

    result = attribute_line("main.py", 1, ledger)
    assert result is not None, (
        "line exists in snapshots — root of snapshots must be returned"
    )
    assert result["event_id"] == ev1.event_id, (
        f"fallback must be the oldest event whose post contains the line "
        f"(ev1), got {result.get('event_id')}"
    )
    assert result["event_id"] != ev2.event_id, (
        "ev2 must NOT be the fallback: ev1 is older and its post also "
        "contains the line"
    )
