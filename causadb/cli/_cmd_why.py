"""`causadb why <file>:<line>` subcommand (F.12.2) — thin wrapper around
`causadb._causal_attrib.attribute_line`.
"""
import json
from typing import Tuple

from causadb._causal_attrib import attribute_line


def _parse_target(target: str) -> Tuple[str, int]:
    """Parse ``"file:line"`` into ``(file, line_int)``.

    The file portion may itself contain colons on Windows-style paths, so we
    split on the LAST colon. Raises ``ValueError`` if no colon is present or
    the line portion is not a positive integer.
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


def cmd_why(args) -> Tuple[int, str]:
    """Delegate to ``attribute_line(file, line, ledger)`` and emit JSON."""
    try:
        file_path, line_number = _parse_target(args.target)
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))

    try:
        from causadb._workspace import resolve_ledger, NoWorkspaceError
        ledger = resolve_ledger(args.ledger)
        result = attribute_line(file_path, line_number, ledger)
        if result is None:
            return (0, json.dumps({"introducer": None}, sort_keys=True))
        return (0, json.dumps({"introducer": result}, sort_keys=True, default=str))
    except ValueError as e:
        return (1, json.dumps({"error": str(e), "error_type": "ValueError"}))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )
