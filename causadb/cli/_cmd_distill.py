"""CLI handler for `causadb distill` — expose the distill engine as a command.

Article II: thin wrapper over `causadb._distill.distill`.
"""

import json
from typing import Tuple

from causadb._distill import distill


def cmd_distill(args) -> Tuple[int, str]:
    """Run the distill engine and output skills in the requested format.

    Returns (exit_code, output_str).
    """
    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(getattr(args, "ledger", None))
        result = distill(ledger)
    except Exception as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))

    fmt = getattr(args, "format", "json")
    if fmt == "json":
        return (0, json.dumps(result, sort_keys=True, default=str))
    elif fmt == "md" or fmt == "markdown":
        return (0, _render_markdown(result))
    elif fmt == "terminal":
        return (0, _render_terminal(result))
    else:
        return (1, json.dumps({"error": f"unknown format: {fmt}"}))


def _render_markdown(result: dict) -> str:
    """Render the distill result as a Markdown document."""
    skills = result.get("skills", [])
    if not skills:
        return "# Distill Result\n\nNo skills extracted (empty or insufficient ledger)."

    lines = ["# Distill Result", ""]
    for skill in skills:
        s_type = skill.get("type", "unknown")
        s_name = skill.get("name", "unnamed")
        s_content = skill.get("content", "")
        s_tokens = skill.get("token_count", 0)
        s_conf = skill.get("confidence", 0.0)
        lines.append(f"## {s_name} ({s_type})")
        lines.append(f"- **Tokens:** {s_tokens}")
        lines.append(f"- **Confidence:** {s_conf:.2f}")
        lines.append("")
        lines.append("```")
        lines.append(s_content)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _render_terminal(result: dict) -> str:
    """Render the distill result as plain text for terminal output."""
    skills = result.get("skills", [])
    if not skills:
        return "Distill Result\n==============\nNo skills extracted (empty or insufficient ledger)."

    lines = ["Distill Result", "=============="]
    for skill in skills:
        s_type = skill.get("type", "unknown")
        s_name = skill.get("name", "unnamed")
        s_content = skill.get("content", "")
        s_tokens = skill.get("token_count", 0)
        s_conf = skill.get("confidence", 0.0)
        lines.append(f"\n{s_name} ({s_type})")
        lines.append(f"  Tokens: {s_tokens}")
        lines.append(f"  Confidence: {s_conf:.2f}")
        lines.append("  Content:")
        for line in s_content.splitlines():
            lines.append(f"    {line}")
    return "\n".join(lines)