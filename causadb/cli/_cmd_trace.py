"""CLI handler for `causadb trace <file>:<line>` (F.12.3).

Returns the upstream causal cone of a line — the full tree of events that
transitively contributed to that line via files-read → files-written.

Pattern A: returns `(exit_code, output_str)`. `main.py` is the single
place that calls `print()`. Output is a JSON string.
"""
import json
from typing import Tuple

from causadb._causal_cone import trace_upstream


def _parse_target(target: str) -> Tuple[str, int]:
    """Parse ``"file:line"`` into ``(file, line_int)``.

    The file portion may itself contain colons on Windows-style paths, so
    we split on the LAST colon. Raises ``ValueError`` if no colon is
    present or the line portion is not a positive integer.
    """
    if ":" not in target:
        raise ValueError(
            f"target must be '<file>:<line>', got {target!r} (no colon found)"
        )
    file_part, _, line_part = target.rpartition(":")
    if not file_part:
        raise ValueError(f"target must be '<file>:<line>', got {target!r}")
    try:
        line = int(line_part)
    except ValueError:
        raise ValueError(
            f"line number must be an integer, got {line_part!r}"
        )
    if line < 1:
        raise ValueError(f"line number must be >= 1, got {line}")
    return file_part, line


def cmd_trace(args) -> Tuple[int, str]:
    """Delegate to ``trace_upstream(file, line, ledger)`` and emit JSON."""
    try:
        file_path, line_number = _parse_target(args.target)
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))

    from causadb._workspace import resolve_ledger, NoWorkspaceError
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({
            "error": str(e),
            "error_type": "NoWorkspaceError",
        }))

    try:
        result = trace_upstream(file_path, line_number, ledger)
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )

    # Convert sets to sorted lists for JSON serialisation.
    serialisable = {
        "writer_event": result["writer_event"],
        "cone": result["cone"],
        "visited": sorted(result["visited"]),
        "depth": result["depth"],
    }
    return (0, json.dumps(serialisable, sort_keys=True, default=str))
