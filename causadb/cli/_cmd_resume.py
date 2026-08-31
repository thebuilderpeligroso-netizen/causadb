"""R.1 — `causadb resume` command.

Merges OCB session context (L1 short-term memory) with ReplayEngine
state (L0 ledger replay) into a structured summary that an agent can
use to resume work after a crash or session break.

The summary is returned as JSON and optionally written as a markdown
file (``RESUME.md``) next to the OCB directory.

Design (Article II — thin wrapper, no duplicated logic):
  - OCB provides ``load_session_context()`` — detects first_run,
    abrupt_close, normal_close, preloads last 2 partitions.
  - ReplayEngine provides ``reconstruct_state()`` — replays the full
    ledger hash-chain to rebuild the causal state dict.
  - This module merges both into a single structured summary.
"""

import json
import os
from datetime import datetime
from typing import Optional, Tuple

from causadb._ocb_manager import OCB
from causadb._replay_engine import ReplayEngine
from causadb._ledger_validator import LedgerValidator, ReplayIntegrityError
from causadb._skill_utils import filter_relevant_skills_from_state





def generate_resume(ledger_path: str, state: Optional[dict] = None) -> dict:
    """Generate a structured resume summary for the given ledger.

    Merges OCB session context with ReplayEngine state.

    Args:
        ledger_path: Absolute path to the ledger.
        state: Optional pre-computed replay state. If provided, skips
            the internal replay (used by revive to avoid double replay).

    Returns a dict with:
      - ``session_type``: first_run | abrupt_close | normal_close
    """
    ocb = OCB.for_ledger(ledger_path)
    ocb_ctx = ocb.load_session_context()
    session_type = ocb_ctx.get("session_type", "first_run")

    # Replay the ledger to get the full causal state (or use pre-computed state)
    # Track whether state was explicitly provided (for skills loading logic)
    state_provided = state is not None
    if state is None:
        state = _safe_replay(ledger_path)

    # Build resume hints based on session type + state
    hints = _build_hints(session_type, state, ocb_ctx)

    from causadb._skill_utils import filter_relevant_skills_from_state

    # F.13.4.5 — Load relevant skills and inject into resume
    # Si state viene pre-computado (state_provided=True), leer skills de ahí;
    # si no (legacy path), usar _load_relevant_skills que hace replay ledger-first.
    if state_provided and "skills" in state:
        relevant_skills = filter_relevant_skills_from_state(state, session_type)
    else:
        relevant_skills = _load_relevant_skills(ledger_path, session_type)


    # Extract key info from state for quick access
    last_timestamp = state.get("timestamp")
    events_count = state.get("events_applied", 0)
    files_modified = state.get("files_modified", [])
    llm_invocations = state.get("llm_invocations", [])
    reasoning_steps = state.get("reasoning_steps", [])
    tools_called = state.get("tools_called", [])
    cost_accounted = state.get("cost_accounted", [])

    # Calculate total cost
    total_cost = sum(
        c.get("cost", 0.0) for c in cost_accounted
    )

    # Last file modified
    last_file = files_modified[-1] if files_modified else None
    last_llm = llm_invocations[-1] if llm_invocations else None
    last_reasoning = reasoning_steps[-1] if reasoning_steps else None

    # BIT-14.8 — unique files (distinct paths, not event count)
    UNIQUE_FILES_DISPLAY_LIMIT = 50
    unique_paths = sorted(set(f["path"] for f in files_modified))
    unique_files_truncated = len(unique_paths) > UNIQUE_FILES_DISPLAY_LIMIT

    # BIT-14.8 — unique commands (distinct commands, not event count)
    commands_run = state.get("commands_run", [])
    unique_cmds = sorted(set(c["command"] for c in commands_run))

    # Fase 0 (ajuste 2) — resumen de entrada = último SESSION_SUMMARY del
    # replay del ledger (Art. I: SIEMPRE sale del ledger, nunca del OCB).
    # El ReplayEngine ya reconstruye ``state["session_summaries"]`` desde
    # los eventos SESSION_SUMMARY; si el estado no lo tiene (ledger
    # vacío/corrupto), ``get`` devuelve ``[]`` → entry_summary None.
    session_summaries = state.get("session_summaries", [])
    entry_summary = session_summaries[-1] if session_summaries else None

    # R.4.0 — preloaded_partitions conserva su formato (lista de IDs, backward
    # compat). preloaded_metadata agrega magnitud/rango SIN tocar BlobStore:
    # deriva event_count y first/last_timestamp de ``lines`` ya parseadas con
    # resolve_blobs=False (Art. V — el detalle granular se carga a pedido).
    _preloaded = ocb_ctx.get("preloaded_partitions", []) or []
    _preloaded_meta = []
    for p in _preloaded:
        p_lines = p.get("lines") or []
        first_ts = p_lines[0].get("timestamp") if p_lines else ""
        last_ts = p_lines[-1].get("timestamp") if p_lines else ""
        _preloaded_meta.append({
            "id": p.get("id"),
            "event_count": len(p_lines),
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
        })

    last_session_id = entry_summary.get("session_id") if entry_summary else None
    
    return {
        "session_type": session_type,
        "entry_summary": entry_summary,
        "last_session_id": last_session_id,
        "ocb_summary": ocb_ctx.get("summary", {}),
        "preloaded_partitions": [
            p.get("id") for p in _preloaded
        ],
        "preloaded_metadata": _preloaded_meta,
        "total_partitions": ocb_ctx["total_partitions"],
        "events_count": events_count,
        "last_timestamp": last_timestamp,
        "files_modified": len(files_modified),
        "unique_files_count": len(unique_paths),
        "unique_files": unique_paths[:UNIQUE_FILES_DISPLAY_LIMIT],
        "unique_files_truncated": unique_files_truncated,
        "commands_run_count": len(commands_run),
        "unique_commands_count": len(unique_cmds),
        "unique_commands": unique_cmds[:UNIQUE_FILES_DISPLAY_LIMIT],
        "last_file": last_file,
        "llm_invocations": len(llm_invocations),
        "last_llm": last_llm,
        "reasoning_steps": len(reasoning_steps),
        "last_reasoning": last_reasoning,
        "tools_called": len(tools_called),
        "total_cost_usd": round(total_cost, 6),
        "last_5_files": [f["path"] for f in files_modified[-5:]],
        "last_5_tools": [
            {"tool": t.get("tool_name"), "error": t.get("error")}
            for t in tools_called[-5:]
        ],
        "resume_hints": hints,
        "relevant_skills": relevant_skills,
    }


def _safe_replay(ledger_path: str) -> dict:
    """Replay the ledger, returning empty state on failure (degradación suave)."""
    try:
        validator = LedgerValidator(ledger_path)
        validator.validate_or_raise()
        engine = ReplayEngine(ledger_path)
        return engine.reconstruct_state()
    except (ReplayIntegrityError, Exception):
        return {}


def _load_relevant_skills(
    ledger_path: str,
    session_type: str,
    max_tokens: int = 2048,
) -> list:
    """Load skills relevant to the session type, respecting max_tokens.

    Relevance mapping (roadmap F.13.4.5, line 281):
      - ``abrupt_close`` → skills of type ``decisions`` and ``tool_patterns``
      - ``normal_close`` → skills of type ``file_tree`` and ``conventions``
      - ``first_run``   → no skills (nothing to resume)

    Args:
        ledger_path: path absoluto del ledger.
        session_type: tipo de sesión detectado por OCB.
        max_tokens: presupuesto máximo de tokens para el conjunto
            de skills inyectados. Skills que excedan el presupuesto
            se truncan con ``[...]`` (roadmap line 282).

    Returns:
        Lista de skill dicts (con keys ``skill_id``, ``skill_type``,
        ``skill_name``, ``content``, ``token_count``, ``confidence``,
        ``source_session``, ``timestamp``, ``event_id``). Skills que
        excedan el presupuesto tienen ``content`` truncado y
        ``token_count`` ajustado al espacio restante.

    Notes:
        - Degradación suave (Artículo V): si ``load_skills`` falla
          (ledger corrupto, etc.), retorna ``[]`` — el resume no se
          rompe.
        - Ledger-first (Artículo I): ``load_skills`` SIEMPRE replay el
          ledger (no lee cache). Anti-teatro (Artículo IX): un test
          que mute ``_load_relevant_skills`` para retornar ``[]`` sin
          llamar a ``load_skills`` debe fallar (test #9).
    """
    # first_run: nothing to resume, no skills needed.
    if session_type == "first_run":
        return []

    # Relevance mapping per session_type.
    if session_type == "abrupt_close":
        relevant_types = ["decisions", "tool_patterns"]
    elif session_type == "normal_close":
        relevant_types = ["file_tree", "conventions"]
    else:
        # Unknown session type — load all (defensive default).
        relevant_types = None

    # Ledger-first load. Degradación suave on any failure.
    try:
        from causadb._skill_registry import load_skills
        skills = load_skills(ledger_path, types=relevant_types)
    except Exception:
        return []

    # Truncate skills that exceed max_tokens (roadmap line 282).
    # Walk skills in order; once the budget is exhausted, truncate the
    # next skill to the remaining allowance (if any) and stop.
    result = []
    total_tokens = 0
    for skill in skills:
        skill_tokens = skill.get("token_count", 0)
        if total_tokens + skill_tokens > max_tokens:
            allowed = max_tokens - total_tokens
            if allowed > 0:
                content = skill.get("content", "")
                # Rough char budget: ~4 chars per token.
                truncated = content[: allowed * 4] + "\n[...]"
                skill_copy = dict(skill)
                skill_copy["content"] = truncated
                skill_copy["token_count"] = allowed
                result.append(skill_copy)
            # Budget exhausted — stop adding more skills.
            break
        result.append(skill)
        total_tokens += skill_tokens

    return result


def _build_hints(session_type: str, state: dict, ocb_ctx: dict) -> list:
    """Build actionable resume hints for the agent."""
    hints = []

    if session_type == "first_run":
        hints.append("This is a fresh workspace — no previous session to resume.")
        return hints

    if session_type == "abrupt_close":
        hints.append(
            "Previous session ended abnormally (no clean shutdown detected). "
            "The last actions may be incomplete."
        )

    if session_type == "normal_close":
        ocb_summary = ocb_ctx.get("summary", {})
        if ocb_summary.get("sedimentada"):
            hints.append(
                f"Previous session closed cleanly. Summary: {json.dumps(ocb_summary, ensure_ascii=False)}"
            )

    # File-based hints
    files = state.get("files_modified", [])
    if files:
        last_file = files[-1]
        hints.append(
            f"Last file modified: {last_file.get('path')} "
            f"({last_file.get('action', 'unknown')}) at {last_file.get('timestamp', '?')}"
        )

    # LLM-based hints
    llm_calls = state.get("llm_invocations", [])
    if llm_calls:
        last_llm = llm_calls[-1]
        if last_llm.get("error"):
            hints.append(
                f"WARNING: last LLM call errored: {last_llm['error']}. "
                "The response may be incomplete."
            )
        else:
            hints.append(
                f"Last LLM call used model {last_llm.get('model', '?')} "
                f"at {last_llm.get('timestamp', '?')}."
            )

    # Reasoning hints
    reasoning = state.get("reasoning_steps", [])
    if reasoning:
        hints.append(
            f"Captured {len(reasoning)} reasoning step(s) from the session."
        )

    # Cost hints
    cost = sum(c.get("cost", 0.0) for c in state.get("cost_accounted", []))
    if cost > 0:
        hints.append(f"Total recorded cost this session: ${cost:.4f} USD.")

    # Tool hints
    tools = state.get("tools_called", [])
    if tools:
        errored = [t for t in tools if t.get("error")]
        if errored:
            hints.append(
                f"{len(errored)} tool call(s) had errors. "
                "Review them before retrying."
            )

    if not hints:
        hints.append("No specific hints — the session appears clean.")

    return hints


def generate_resume_markdown(resume_data: dict) -> str:
    """Render the resume summary as a markdown document for RESUME.md."""
    lines = []
    lines.append("# CausaDB Session Resume\n")
    lines.append(f"Generated: {datetime.now().isoformat()}\n")

    st = resume_data.get("session_type", "unknown")
    lines.append(f"**Session type:** `{st}`\n")

    if st == "abrupt_close":
        lines.append("> ⚠️ Previous session ended abnormally. Last actions may be incomplete.\n")

    lines.append("## Summary\n")
    lines.append(f"- **Events applied:** {resume_data.get('events_count', 0)}")
    lines.append(f"- **Files modified:** {resume_data.get('files_modified', 0)}")
    lines.append(f"- **LLM invocations:** {resume_data.get('llm_invocations', 0)}")
    lines.append(f"- **Reasoning steps:** {resume_data.get('reasoning_steps', 0)}")
    lines.append(f"- **Tools called:** {resume_data.get('tools_called', 0)}")
    lines.append(f"- **Total cost:** ${resume_data.get('total_cost_usd', 0):.4f} USD")
    lines.append(f"- **Last timestamp:** {resume_data.get('last_timestamp', 'N/A')}\n")

    last_file = resume_data.get("last_file")
    if last_file:
        lines.append("## Last File Modified\n")
        lines.append(f"- **Path:** `{last_file.get('path', '?')}`")
        lines.append(f"- **Action:** {last_file.get('action', '?')}")
        lines.append(f"- **Timestamp:** {last_file.get('timestamp', '?')}\n")

    last_llm = resume_data.get("last_llm")
    if last_llm:
        lines.append("## Last LLM Invocation\n")
        lines.append(f"- **Model:** {last_llm.get('model', '?')}")
        lines.append(f"- **Timestamp:** {last_llm.get('timestamp', '?')}")
        if last_llm.get("error"):
            lines.append(f"- **Error:** {last_llm['error']}")
        lines.append("")

    last_files = resume_data.get("last_5_files", [])
    if last_files:
        lines.append("## Last 5 Files Modified\n")
        for f in last_files:
            lines.append(f"- `{f}`")
        lines.append("")

    last_tools = resume_data.get("last_5_tools", [])
    if last_tools:
        lines.append("## Last 5 Tool Calls\n")
        for t in last_tools:
            tool = t.get("tool", "?")
            err = t.get("error")
            if err:
                lines.append(f"- `{tool}` — ERROR: {err}")
            else:
                lines.append(f"- `{tool}`")
        lines.append("")

    hints = resume_data.get("resume_hints", [])
    if hints:
        lines.append("## Resume Hints\n")
        for h in hints:
            lines.append(f"- {h}")
        lines.append("")

    # F.13.4.5 — Relevant Skills section
    relevant_skills = resume_data.get("relevant_skills", [])
    if relevant_skills:
        lines.append("## Relevant Skills\n")
        for skill in relevant_skills:
            name = skill.get("skill_name", "unnamed")
            stype = skill.get("skill_type", "unknown")
            tokens = skill.get("token_count", 0)
            confidence = skill.get("confidence", 0.0)
            lines.append(
                f"**{name}** ({stype}, tokens: {tokens}, confidence: {confidence})\n"
            )
            content = skill.get("content", "")
            lines.append(f"```\n{content}\n```\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------

def cmd_resume(args) -> Tuple[int, str]:
    """Handle ``causadb resume`` — generate and optionally write RESUME.md."""
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger_path = resolve_ledger(args.ledger)
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))

    try:
        resume_data = generate_resume(ledger_path)
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))

    # Optionally write RESUME.md
    write_md = getattr(args, "write", False)
    if write_md:
        resume_dir = os.path.join(os.path.dirname(ledger_path), "ocb")
        os.makedirs(resume_dir, exist_ok=True)
        md_path = os.path.join(resume_dir, "RESUME.md")
        md_content = generate_resume_markdown(resume_data)
        with open(md_path, "w") as f:
            f.write(md_content)
            f.flush()
            os.fsync(f.fileno())
        resume_data["resume_md_path"] = md_path

    output_format = getattr(args, "format", "json")
    if output_format == "markdown":
        return (0, generate_resume_markdown(resume_data))

    return (0, json.dumps(resume_data, indent=2, default=str))
