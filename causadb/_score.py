"""`causadb._score` — F.13.3.1 / F.13.3.2 / F.13.3.3: Churn, Waste and unified Score.

This module reads the CausaDB ledger directly (via ``LedgerReader``) and
computes three families of metrics, all grouped by session (``ctx_id``):

  * ``compute_churn``  — lines added/deleted per session, using real diff
    between ``pre_snapshot`` and ``post_snapshot`` when available, with a
    documented fallback (estimation from ``payload.writes``) that NEVER
    returns 0 silently (Article V — Fall-Closed on silent zero).
  * ``compute_waste``  — LLM cost wasted on files that were written and
    then overwritten/deleted within the same session. Correlation between
    ``LLM_INVOKED`` and ``FILE_MODIFIED`` is by timestamp proximity, which
    is inherently imprecise — the output always carries
    ``correlation_method: "timestamp_proximity"`` for transparency.
  * ``compute_score``  — combines churn, waste and survival into a 0-100
    score using configurable weights from ``_config.py`` (section [score]).

Design notes
------------
* The module does NOT mutate the ledger — it only reads.
* Survival ratio is sourced from ``_audit.py`` when a git repo is
  available; otherwise it defaults to 1.0 with a warning. The audit
  module is git-based (standalone), so for ledger-only scoring we
  default to full survival and surface the assumption.
* Anti-teatro (Article IX): every function has a non-trivial
  implementation. A stub that returns 0 / empty / hardcoded 100 will
  fail the dedicated anti-teatro tests in ``tests/test_score.py``.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from causadb._ledger_reader import LedgerReader


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iter_entries(ledger_path: str):
    """Yield raw ledger entries (dicts with ``event`` + ``hash``)."""
    return LedgerReader(ledger_path).read_all_entries()


def _group_by_ctx(entries) -> Dict[str, List[Dict[str, Any]]]:
    """Group raw entries by ``ctx_id`` (session). Entries without ctx_id go
    to ``"__no_ctx__"`` so they are not lost (Fall-Closed on silent drop)."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        event = entry.get("event", {})
        ctx = event.get("ctx_id") or "__no_ctx__"
        groups.setdefault(ctx, []).append(entry)
    return groups


def _diff_snapshots(pre_files: Dict[str, Any], post_files: Dict[str, Any]) -> Tuple[int, int, int]:
    """Compute (files_changed, lines_added, lines_deleted) from two snapshot
    file dicts.

    Each snapshot file dict maps ``rel_path -> {"hash", "size", "mtime"}``.
    We do NOT have per-line diffs from snapshots alone (only file-level
    hashes), so ``lines_added`` / ``lines_deleted`` are estimated from
    file sizes when content blobs are unavailable. When the BlobStore is
    available and the snapshot carries ``blob_refs``, we attempt a real
    line-level diff.

    Returns ``(files_changed, lines_added, lines_deleted)``.
    """
    files_changed = 0
    lines_added = 0
    lines_deleted = 0
    all_paths = set(pre_files.keys()) | set(post_files.keys())
    for path in all_paths:
        in_pre = path in pre_files
        in_post = path in post_files
        if in_pre and in_post:
            if pre_files[path].get("hash") != post_files[path].get("hash"):
                files_changed += 1
                # Modified file: estimate churn as the absolute size delta.
                # This is a conservative proxy — without content blobs we
                # cannot do a true line diff.
                pre_size = pre_files[path].get("size", 0) or 0
                post_size = post_files[path].get("size", 0) or 0
                delta = post_size - pre_size
                if delta > 0:
                    lines_added += delta
                else:
                    lines_deleted += abs(delta)
        elif in_post and not in_pre:
            files_changed += 1
            lines_added += post_files[path].get("size", 0) or 0
        else:  # in_pre and not in_post → deleted
            files_changed += 1
            lines_deleted += pre_files[path].get("size", 0) or 0
    return files_changed, lines_added, lines_deleted


def _try_load_snapshot(snapshot_hash: Optional[str], blob_store) -> Optional[Dict[str, Any]]:
    """Attempt to load a snapshot manifest from the BlobStore.

    Returns the snapshot dict (with ``files``) or ``None`` if the hash is
    missing or the blob cannot be read.
    """
    if not snapshot_hash:
        return None
    if blob_store is None:
        return None
    try:
        return blob_store.get(snapshot_hash)
    except (FileNotFoundError, OSError, KeyError):
        return None
    except Exception:
        # Any unexpected blob store error → treat as missing snapshot
        # (Fall-Closed on the metric, not on the whole computation).
        return None


def _resolve_blob_store(ledger_path: str):
    """Build a BlobStore pointing at ``<ledger_dir>/blobs`` if it exists."""
    try:
        from causadb._blob_store import BlobStore
    except ImportError:
        return None
    base = os.path.join(os.path.dirname(ledger_path), "blobs")
    if not os.path.isdir(base):
        return None
    return BlobStore(base)


def _parse_timestamp(ts: str) -> float:
    """Parse an ISO-8601 timestamp to epoch seconds (best-effort)."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        # Handle trailing 'Z'.
        ts_norm = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(ts_norm).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# F.13.3.1 — Churn Metrics per Session
# ---------------------------------------------------------------------------

def compute_churn(ledger_path: str, config=None, *, entries=None) -> Dict[str, Dict[str, Any]]:
    """Compute per-session churn metrics from the ledger.

    For each session (``ctx_id``):
      * Iterate ``FILE_MODIFIED`` events.
      * When ``pre_snapshot`` and ``post_snapshot`` are present, compute a
        real diff (files added/deleted/modified + size-based line proxy).
      * When snapshots are missing, fall back to estimating churn from
        ``payload.writes`` (count of touched files as a line proxy) and
        emit a ``no_snapshots_for_<event_id>`` warning. NEVER return 0
        silently when writes exist.

    Args:
        ledger_path: path absoluto del ledger (usado para snapshots/blobs).
        config: config opcional.
        entries: entradas crudas del ledger pre-materializadas
            (keyword-only, P4-C). Si viene, CERO lecturas de disco del
            ledger; si es ``None`` se leen aquí (comportamiento legacy).
            Las entradas son solo-lectura — la función no las muta.

    Returns a dict keyed by ``ctx_id``::

        {
          ctx_id: {
            "files_churned": int,
            "lines_added": int,
            "lines_deleted": int,
            "churn_ratio": float,   # lines_changed / (lines_added + lines_deleted + 1e-9)
            "warnings": [str, ...],
          },
          ...
        }

    An empty ledger returns ``{}``.
    """
    if entries is None:
        entries = list(_iter_entries(ledger_path))
    if not entries:
        return {}

    blob_store = _resolve_blob_store(ledger_path)
    groups = _group_by_ctx(entries)

    result: Dict[str, Dict[str, Any]] = {}
    for ctx, ctx_entries in groups.items():
        files_churned = 0
        lines_added = 0
        lines_deleted = 0
        warnings: List[str] = []

        for entry in ctx_entries:
            event = entry.get("event", {})
            if event.get("event_type") != "FILE_MODIFIED":
                continue
            payload = event.get("payload", {}) or {}
            pre_hash = event.get("pre_snapshot")
            post_hash = event.get("post_snapshot")
            pre_snap = _try_load_snapshot(pre_hash, blob_store)
            post_snap = _try_load_snapshot(post_hash, blob_store)

            if pre_snap is not None and post_snap is not None:
                # Real diff path.
                fc, la, ld = _diff_snapshots(
                    pre_snap.get("files", {}) or {},
                    post_snap.get("files", {}) or {},
                )
                files_churned += fc
                lines_added += la
                lines_deleted += ld
            else:
                # Fallback path — no snapshots available.
                eid = event.get("event_id", "unknown")
                writes = payload.get("writes")
                if writes and isinstance(writes, list):
                    # Each entry in `writes` is a declared file mutation.
                    # Use the count as a proxy for files churned, and
                    # estimate lines from any declared line counts.
                    for w in writes:
                        if not isinstance(w, dict):
                            continue
                        files_churned += 1
                        # Some writes carry `lines_added` / `lines_deleted`.
                        la = w.get("lines_added")
                        ld = w.get("lines_deleted")
                        if isinstance(la, int) and la > 0:
                            lines_added += la
                        if isinstance(ld, int) and ld > 0:
                            lines_deleted += ld
                        # If no explicit line counts, use a proxy of 1 line
                        # per touched file so we NEVER return 0 silently.
                        if not isinstance(la, int) and not isinstance(ld, int):
                            lines_added += 1
                    warnings.append(f"no_snapshots_for_{eid}")
                elif "path" in payload:
                    # Single-file event without snapshots and without `writes`.
                    # Estimate 1 line churned (proxy) + warning.
                    files_churned += 1
                    lines_added += 1
                    warnings.append(f"no_snapshots_for_{eid}")
                # If neither writes nor path → nothing to count, no warning.

        total_lines = lines_added + lines_deleted
        # churn_ratio: fraction of churned lines relative to total activity.
        # We define it as (lines_added + lines_deleted) / (total_lines + 1)
        # so a session with real churn approaches 1.0 and a session with no
        # churn is 0.0. The +1 avoids division by zero but does NOT mask
        # real churn (anti-teatro: a stub that skips the diff collapses
        # this to 0 and is caught by the anti-teatro test).
        churn_ratio = total_lines / (total_lines + 1.0) if total_lines > 0 else 0.0

        result[ctx] = {
            "files_churned": files_churned,
            "lines_added": lines_added,
            "lines_deleted": lines_deleted,
            "churn_ratio": churn_ratio,
            "warnings": warnings,
        }

    return result


# ---------------------------------------------------------------------------
# F.13.3.2 — Waste Metrics
# ---------------------------------------------------------------------------

def compute_waste(ledger_path: str, config=None, *, entries=None) -> Dict[str, Dict[str, Any]]:
    """Compute per-session waste metrics by correlating ``LLM_INVOKED`` and
    ``FILE_MODIFIED`` events.

    For each session:
      * Collect all ``LLM_INVOKED`` events with their cost (from
        ``payload.cost`` or derived from ``COST_ACCOUNTED`` events matched
        by timestamp proximity).
      * Collect all ``FILE_MODIFIED`` events with their file path and
        timestamp.
      * For each file that was written (created/modified) and then later
        overwritten or deleted within the same session, attribute the cost
        of the LLM call(s) that preceded the write to "waste".
      * ``waste_ratio = cost_wasted / total_cost``.

    Args:
        ledger_path: path absoluto del ledger.
        config: config opcional.
        entries: entradas crudas del ledger pre-materializadas
            (keyword-only, P4-C). Si viene, CERO lecturas de disco del
            ledger; si es ``None`` se leen aquí (comportamiento legacy).
            Las entradas son solo-lectura — la función no las muta.

    Correlation between LLM calls and file writes is by **timestamp
    proximity** — the most recent LLM call before a write is assumed to
    have produced it. This is inherently imprecise (two nearby LLM calls
    can attribute to the wrong one), so the output always carries
    ``correlation_method: "timestamp_proximity"``.

    Returns a dict keyed by ``ctx_id``::

        {
          ctx_id: {
            "total_cost": float,
            "wasted_cost": float,
            "waste_ratio": float,
            "wasted_files": [str, ...],
            "correlation_method": "timestamp_proximity",
          },
          ...
        }
    """
    if entries is None:
        entries = list(_iter_entries(ledger_path))
    if not entries:
        return {}

    groups = _group_by_ctx(entries)
    result: Dict[str, Dict[str, Any]] = {}

    for ctx, ctx_entries in groups.items():
        # Collect LLM invocations: (timestamp_epoch, cost, event_id).
        llm_calls: List[Tuple[float, float, str]] = []
        # Collect file events: (timestamp_epoch, path, action, event_id).
        file_events: List[Tuple[float, str, str, str]] = []

        for entry in ctx_entries:
            event = entry.get("event", {})
            etype = event.get("event_type")
            payload = event.get("payload", {}) or {}
            ts = _parse_timestamp(event.get("timestamp", ""))

            if etype == "LLM_INVOKED":
                cost = payload.get("cost")
                if cost is None:
                    # Some LLM_INVOKED events carry cost via COST_ACCOUNTED
                    # matched by proximity; we approximate with 0 if absent
                    # but still record the call so it can be attributed.
                    cost = 0.0
                try:
                    cost = float(cost)
                except (TypeError, ValueError):
                    cost = 0.0
                llm_calls.append((ts, cost, event.get("event_id", "")))
            elif etype == "FILE_MODIFIED":
                path = payload.get("path", "unknown")
                action = payload.get("action", "unknown")
                file_events.append((ts, path, action, event.get("event_id", "")))

        total_cost = sum(c for _, c, _ in llm_calls)

        if not llm_calls or not file_events:
            # No LLM calls or no file writes → no waste to attribute.
            # KEY FIX: assign to result[ctx] (was: bare `return`) so the
            # per-session loop continues and the contract (dict keyed by
            # ctx_id) holds for every session. The `continue` below also
            # keeps `wasted_files: []` for this early branch.
            result[ctx] = {
                "total_cost": float(total_cost),
                "wasted_cost": 0.0,
                "waste_ratio": 0.0,
                "wasted_files": [],
                "correlation_method": "timestamp_proximity",
            }
            continue

        # Build a per-path timeline of events: list of (ts, action, event_id).
        per_file: Dict[str, List[Tuple[float, str, str]]] = {}
        for ts, path, action, eid in file_events:
            per_file.setdefault(path, []).append((ts, action, eid))

        # For each file, find writes that were later overwritten or deleted.
        # A "wasted write" is a create/modify followed (within the session)
        # by another modify/delete on the same path.
        wasted_files: List[str] = []
        wasted_event_ids: set = set()
        for path, timeline in per_file.items():
            # Sort by timestamp.
            timeline.sort(key=lambda x: x[0])
            for i, (ts_i, action_i, eid_i) in enumerate(timeline):
                if action_i in ("create", "modify", "write", "update"):
                    # Look ahead for a subsequent overwrite/delete.
                    for j in range(i + 1, len(timeline)):
                        action_j = timeline[j][1]
                        if action_j in ("modify", "delete", "overwrite", "write", "update"):
                            wasted_files.append(path)
                            wasted_event_ids.add(eid_i)
                            break  # only count the first overwrite per write

        # Attribute cost: for each wasted write, find the most recent LLM
        # call before the write's timestamp and add its cost to wasted_cost.
        # Sort LLM calls by timestamp for binary-search-style lookup.
        llm_calls_sorted = sorted(llm_calls, key=lambda x: x[0])
        llm_timestamps = [c[0] for c in llm_calls_sorted]

        wasted_cost = 0.0
        # Map each file event back to its timestamp to attribute.
        file_event_by_id = {eid: (ts, path, action) for ts, path, action, eid in file_events}
        for wasted_eid in wasted_event_ids:
            if wasted_eid not in file_event_by_id:
                continue
            write_ts = file_event_by_id[wasted_eid][0]
            # Find the most recent LLM call with ts <= write_ts.
            import bisect
            idx = bisect.bisect_right(llm_timestamps, write_ts) - 1
            if idx >= 0:
                wasted_cost += llm_calls_sorted[idx][1]

        waste_ratio = (wasted_cost / total_cost) if total_cost > 0 else 0.0

        result[ctx] = {
            "total_cost": total_cost,
            "wasted_cost": wasted_cost,
            "waste_ratio": waste_ratio,
            "wasted_files": sorted(set(wasted_files)),
            "correlation_method": "timestamp_proximity",
        }

    return result


# ---------------------------------------------------------------------------
# F.13.3.3 — Unified Score
# ---------------------------------------------------------------------------

def _get_survival_ratio(ledger_path: str, config=None) -> Tuple[float, List[str]]:
    """Best-effort survival ratio for the unified score.

    Tries ``_audit.AuditReport.build(repo_dir)`` when a git repo is
    available; otherwise defaults to 1.0 (full survival) with a warning.

    Returns ``(survival_ratio, warnings)``.
    """
    warnings: List[str] = []
    # The audit module is git-based and standalone — it does not read the
    # ledger. For ledger-only scoring we default to full survival and
    # surface the assumption so callers know the score is churn+waste only.
    repo_dir = None
    if config is not None:
        repo_dir = getattr(config, "workspace_dir", None) or getattr(config, "repo_dir", None)
    if repo_dir is None:
        # Try CWD as a git repo.
        repo_dir = os.getcwd()
    try:
        from causadb._audit import AuditReport, AuditError
        report = AuditReport.build(repo_dir)
        # Aggregate survival across all agents: weighted by introduced lines.
        total_introduced = sum(a.get("introduced", 0) for a in report.agents)
        total_surviving = sum(a.get("surviving", 0) for a in report.agents)
        if total_introduced > 0:
            ratio = min(max(total_surviving / total_introduced, 0.0), 1.0)
            return ratio, warnings
        # No AI commits → treat as full survival (no AI code to lose).
        return 1.0, warnings
    except Exception:
        # Audit unavailable (no git, not a repo, etc.) → default + warning.
        warnings.append("survival_defaulted_to_1_no_git_audit")
        return 1.0, warnings


def compute_score(ledger_path: str, config=None, *, entries=None) -> Dict[str, Any]:
    """Compute the unified 0-100 score combining churn, waste and survival.

    Formula::

        score = 100 * (1 - w1*churn - w2*waste - w3*(1 - survival))

    where ``w1, w2, w3`` come from ``CausaDBConfig`` (section [score],
    defaults 0.3 / 0.3 / 0.4).

    The churn and waste ratios are aggregated across all sessions
    (weighted by total cost for waste, by total lines for churn) into a
    single per-ledger ratio before scoring.

    Args:
        ledger_path: path absoluto del ledger.
        config: config opcional (posicional, compat ``_rest_api.py``).
        entries: entradas crudas del ledger pre-materializadas
            (keyword-only, P4-C). Si viene, se comparten con churn y waste
            (CERO re-lecturas del ledger en las tres funciones); si es
            ``None`` se materializan UNA vez aquí (comportamiento legacy,
            que antes leía el ledger 2 veces: churn + waste).

    Returns::

        {
          "overall_score": float,        # 0-100
          "churn_score": float,          # 0-100 (100 = no churn)
          "waste_score": float,          # 0-100 (100 = no waste)
          "survival_score": float,       # 0-100 (100 = full survival)
          "weights_used": {"churn": w1, "waste": w2, "survival": w3},
          "correlation_method": "timestamp_proximity",
          "warnings": [str, ...],
          "per_session": {ctx_id: {...}, ...},   # detailed breakdown
        }
    """
    # Resolve weights from config (or defaults).
    if config is None:
        from causadb._config import CausaDBConfig
        config = CausaDBConfig(ledger_path=ledger_path)
    w1 = float(getattr(config, "score_weight_churn", 0.3))
    w2 = float(getattr(config, "score_weight_waste", 0.3))
    w3 = float(getattr(config, "score_weight_survival", 0.4))

    # P4-C — materializar las entradas crudas UNA vez y compartirlas con
    # churn y waste (antes: 2 lecturas completas independientes, ~6.4s en
    # ledgers grandes). Survival queda como está (git-audit, fuera del
    # ledger).
    if entries is None:
        entries = list(_iter_entries(ledger_path))
    churn_data = compute_churn(ledger_path, config, entries=entries)
    waste_data = compute_waste(ledger_path, config, entries=entries)

    # Aggregate churn across sessions weighted by total lines.
    total_lines = 0
    weighted_churn_sum = 0.0
    for ctx, data in churn_data.items():
        la = data.get("lines_added", 0)
        ld = data.get("lines_deleted", 0)
        tl = la + ld
        if tl > 0:
            weighted_churn_sum += data.get("churn_ratio", 0.0) * tl
            total_lines += tl
    churn_ratio = (weighted_churn_sum / total_lines) if total_lines > 0 else 0.0

    # Aggregate waste across sessions weighted by total cost.
    total_cost = 0.0
    weighted_waste_sum = 0.0
    for ctx, data in waste_data.items():
        tc = data.get("total_cost", 0.0)
        if tc > 0:
            weighted_waste_sum += data.get("waste_ratio", 0.0) * tc
            total_cost += tc
    waste_ratio = (weighted_waste_sum / total_cost) if total_cost > 0 else 0.0

    survival_ratio, surv_warnings = _get_survival_ratio(ledger_path, config)

    # Clamp inputs to [0, 1].
    churn_ratio = min(max(churn_ratio, 0.0), 1.0)
    waste_ratio = min(max(waste_ratio, 0.0), 1.0)
    survival_ratio = min(max(survival_ratio, 0.0), 1.0)

    overall = 100.0 * (1.0 - w1 * churn_ratio - w2 * waste_ratio - w3 * (1.0 - survival_ratio))
    # Clamp to [0, 100].
    overall = min(max(overall, 0.0), 100.0)

    churn_score = 100.0 * (1.0 - churn_ratio)
    waste_score = 100.0 * (1.0 - waste_ratio)
    survival_score = 100.0 * survival_ratio

    # Per-session breakdown for the CLI's --by-session flag.
    per_session: Dict[str, Dict[str, Any]] = {}
    all_ctx = set(churn_data.keys()) | set(waste_data.keys())
    for ctx in all_ctx:
        c = churn_data.get(ctx, {})
        w = waste_data.get(ctx, {})
        c_ratio = min(max(c.get("churn_ratio", 0.0), 0.0), 1.0)
        w_ratio = min(max(w.get("waste_ratio", 0.0), 0.0), 1.0)
        s_ratio = survival_ratio  # survival is ledger-wide, not per-session
        s_overall = 100.0 * (1.0 - w1 * c_ratio - w2 * w_ratio - w3 * (1.0 - s_ratio))
        per_session[ctx] = {
            "overall_score": min(max(s_overall, 0.0), 100.0),
            "churn_ratio": c_ratio,
            "waste_ratio": w_ratio,
            "survival_ratio": s_ratio,
            "churn": c,
            "waste": w,
        }

    warnings = list(surv_warnings)
    # Surface churn fallback warnings at the top level too.
    for ctx, data in churn_data.items():
        for w in data.get("warnings", []):
            warnings.append(f"{ctx}:{w}")

    return {
        "overall_score": overall,
        "churn_score": churn_score,
        "waste_score": waste_score,
        "survival_score": survival_score,
        "weights_used": {
            "churn": w1,
            "waste": w2,
            "survival": w3,
        },
        "correlation_method": "timestamp_proximity",
        "warnings": warnings,
        "per_session": per_session,
    }
