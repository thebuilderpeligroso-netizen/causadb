"""CLI handler for `causadb audit` (F.12.6).

Standalone command — measures AI-authored code survival in the git history
of any repository. Does NOT require a `.causadb/` workspace or a ledger.

Pattern A: returns `(exit_code, output_str)`. `main.py` is the single place
that calls `print()`. If `--output` is given, writes the rendered report to
that file and returns a small JSON metadata blob; otherwise returns the
report content directly.
"""

import json
import os
from typing import Tuple

from causadb._audit import AuditReport, AuditError


def cmd_audit(args) -> Tuple[int, str]:
    fmt = args.format
    output_path = args.output
    repo = args.repo or os.getcwd()

    try:
        report = AuditReport.build(repo)
        if fmt == "markdown":
            content = report.to_markdown()
        elif fmt == "json":
            content = report.to_json()
        elif fmt == "terminal":
            content = report.to_terminal()
        else:
            return (1, json.dumps({"error": f"unknown format: {fmt}"}))

        if output_path:
            with open(output_path, "w") as f:
                f.write(content)
            return (
                0,
                json.dumps({
                    "written_to": output_path,
                    "format": fmt,
                    "bytes": len(content),
                }, sort_keys=True),
            )
        return (0, content)
    except AuditError as e:
        return (1, json.dumps({"error": str(e), "error_type": "AuditError"}))
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )
