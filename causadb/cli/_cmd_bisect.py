"""CLI handler for `causadb bisect --test cmd` (F.12.5).

Binary search over the event chain to find the first action that broke
the test command. Restores each candidate event's ``post_snapshot`` to
the workspace, runs the test command, and narrows the search by exit code.

Pattern A: returns ``(exit_code, output_str)``. ``main.py`` is the single
place that calls ``print()``.

The workspace dir is resolved from the workspace config (``watch_dirs[0]``
or the project root). The ledger is resolved via ``--ledger`` or
auto-discovery. The test command is run with ``cwd=workspace``.
"""

import json
import os
from typing import Tuple

from causadb._bisect import bisect, BisectError
from causadb._workspace import resolve_ledger, NoWorkspaceError


def _resolve_watch_dir(ledger_path: str) -> str:
    """Resolve the workspace dir from the workspace config adjacent to the ledger.

    The workspace config lives at ``<ledger_dir>/../config.json`` (i.e. the
    ``.causadb/`` dir). Its ``watch_dirs[0]`` is the workspace root. If no
    config or no watch_dirs, fall back to the ledger's parent directory.
    """
    ledger_dir = os.path.dirname(os.path.abspath(ledger_path))
    # .causadb/ contains ledger.log; the project root is one level up.
    project_root = os.path.dirname(ledger_dir)
    config_path = os.path.join(project_root, ".causadb", "config.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            watch_dirs = cfg.get("watch_dirs") or []
            if watch_dirs:
                return watch_dirs[0]
        except (json.JSONDecodeError, OSError):
            pass
    return project_root


def cmd_bisect(args) -> Tuple[int, str]:
    test_cmd = args.test
    ledger = args.ledger

    if not test_cmd:
        return (1, json.dumps({"error": "--test is required"}))

    try:
        ledger = resolve_ledger(ledger)
    except NoWorkspaceError:
        return (
            1,
            json.dumps({
                "error": "No .causadb/ workspace found and no --ledger provided.",
            }),
        )

    watch_dir = _resolve_watch_dir(ledger)

    try:
        result = bisect(test_cmd, ledger, watch_dir)
    except BisectError as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": "BisectError"}),
        )
    except Exception as e:
        return (
            1,
            json.dumps({"error": str(e), "error_type": type(e).__name__}),
        )

    if result is None:
        return (
            0,
            json.dumps({
                "result": "all_pass",
                "message": "All events pass the test command.",
            }, sort_keys=True),
        )

    return (
        0,
        json.dumps({
            "result": "first_bad",
            "event": result,
        }, sort_keys=True),
    )
