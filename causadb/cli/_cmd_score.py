"""CLI handler for `causadb score` (F.13.3.4).

Thin wrapper (Article II) — delegates all logic to `causadb._score`.
Pattern A: returns `(exit_code, output_str)`. `main.py` is the single
place that calls `print()`.

Usage::

    causadb score [--ledger PATH] [--format json|md|terminal]
    causadb score --session CTX_ID
    causadb score --by-session
"""
import json
from typing import Tuple

from causadb._score import compute_score


def cmd_score(args) -> Tuple[int, str]:
    """Delegate to `causadb._score.compute_score` and render the output."""
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        result = compute_score(ledger)
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    fmt = getattr(args, "format", "json")
    session_filter = getattr(args, "session", None)
    by_session = getattr(args, "by_session", False)

    # Filter to a single session if requested.
    if session_filter:
        per = result.get("per_session", {})
        if session_filter not in per:
            return (1, json.dumps({
                "error": f"session '{session_filter}' not found in ledger",
                "available_sessions": sorted(per.keys()),
            }))
        single = per[session_filter]
        result = {
            "session_id": session_filter,
            "overall_score": single["overall_score"],
            "churn_ratio": single["churn_ratio"],
            "waste_ratio": single["waste_ratio"],
            "survival_ratio": single["survival_ratio"],
            "churn": single["churn"],
            "waste": single["waste"],
            "weights_used": result["weights_used"],
            "correlation_method": result["correlation_method"],
            "warnings": result.get("warnings", []),
        }

    if fmt == "json":
        return (0, json.dumps(result, sort_keys=True, default=str))
    elif fmt == "md" or fmt == "markdown":
        return (0, _render_markdown(result, by_session=by_session))
    elif fmt == "terminal":
        return (0, _render_terminal(result, by_session=by_session))
    else:
        return (1, json.dumps({"error": f"unknown format: {fmt}"}))


def _render_markdown(result: dict, by_session: bool = False) -> str:
    lines = []
    lines.append("## Score")
    lines.append("")
    lines.append(f"- **Overall:** `{result['overall_score']:.1f}/100`")
    lines.append(f"- **Churn score:** `{result['churn_score']:.1f}/100`")
    lines.append(f"- **Waste score:** `{result['waste_score']:.1f}/100`")
    lines.append(f"- **Survival score:** `{result['survival_score']:.1f}/100`")
    w = result.get("weights_used", {})
    lines.append(
        f"- **Weights:** churn={w.get('churn', 0.3)}, "
        f"waste={w.get('waste', 0.3)}, survival={w.get('survival', 0.4)}"
    )
    cm = result.get("correlation_method", "timestamp_proximity")
    lines.append(f"- **Correlation method:** `{cm}`")
    if cm == "timestamp_proximity":
        lines.append("")
        lines.append(
            "> ⚠ **Advertencia:** la correlación LLM↔archivo es por "
            "*timestamp proximity*, inherentemente imprecisa. Ver "
            "`correlation_method` en el JSON para detalle."
        )
    warnings = result.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append("### Warnings")
        for wn in warnings:
            lines.append(f"- {wn}")
    if by_session:
        per = result.get("per_session", {})
        if per:
            lines.append("")
            lines.append("### Per-session breakdown")
            lines.append("")
            lines.append("| Session | Overall | Churn ratio | Waste ratio | Survival |")
            lines.append("|---------|---------|--------------|--------------|----------|")
            for ctx, s in sorted(per.items()):
                lines.append(
                    f"| {ctx} | {s['overall_score']:.1f} | "
                    f"{s['churn_ratio']:.3f} | {s['waste_ratio']:.3f} | "
                    f"{s['survival_ratio']:.3f} |"
                )
    return "\n".join(lines)


def _render_terminal(result: dict, by_session: bool = False) -> str:
    lines = []
    lines.append("=== CausaDB Score ===")
    lines.append(f"Overall:  {result['overall_score']:.1f}/100")
    lines.append(f"Churn:    {result['churn_score']:.1f}/100")
    lines.append(f"Waste:    {result['waste_score']:.1f}/100")
    lines.append(f"Survival: {result['survival_score']:.1f}/100")
    w = result.get("weights_used", {})
    lines.append(
        f"Weights:  churn={w.get('churn', 0.3)} "
        f"waste={w.get('waste', 0.3)} "
        f"survival={w.get('survival', 0.4)}"
    )
    cm = result.get("correlation_method", "timestamp_proximity")
    lines.append(f"Correlation: {cm}")
    if cm == "timestamp_proximity":
        lines.append(
            "  ⚠ Advertencia: correlación LLM↔archivo por timestamp "
            "proximity (imprecisa)."
        )
    warnings = result.get("warnings", [])
    if warnings:
        lines.append("Warnings:")
        for wn in warnings:
            lines.append(f"  - {wn}")
    if by_session:
        per = result.get("per_session", {})
        if per:
            lines.append("")
            lines.append("Per-session:")
            for ctx, s in sorted(per.items()):
                lines.append(
                    f"  {ctx:<30} overall={s['overall_score']:.1f} "
                    f"churn={s['churn_ratio']:.3f} "
                    f"waste={s['waste_ratio']:.3f} "
                    f"survival={s['survival_ratio']:.3f}"
                )
    return "\n".join(lines)
