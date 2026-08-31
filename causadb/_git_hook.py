"""F.11.3 — Git hook COMMIT_MADE automático.

Installs a post-commit git hook that logs a COMMIT_MADE event to the CausaDB
ledger on every git commit.

Artículo I: The hook invokes `causadb log` (which uses LedgerWriter.append()).
Never opens ledger.log directly.

Artículo VII: Lo mínimo funcional — single hook file, bash script.
"""

import os
import stat
from typing import Optional


HOOK_TEMPLATE = """#!/bin/bash
# Post-commit hook installed by CausaDB
HASH=$(git rev-parse HEAD)
MSG=$(git log -1 --format=%s)
python -m causadb.cli.main log '{{"event_type":"COMMIT_MADE","ctx_id":"git","source":"causadb:git-hook","payload":{{"commit_hash":"'"$HASH"'","message":"'"$MSG"'"}}}}' --ledger {ledger_path}
"""


def install_post_commit_hook(
    git_dir: str,
    ledger_path: str,
) -> bool:
    """Install a post-commit hook that logs COMMIT_MADE on each commit.

    Args:
        git_dir: Root of the git repository (contains .git/).
        ledger_path: Absolute path to ledger.log.

    Returns:
        True if hook was installed, False if one already existed.
    """
    hooks_dir = os.path.join(git_dir, ".git", "hooks")
    hook_path = os.path.join(hooks_dir, "post-commit")

    if os.path.exists(hook_path):
        return False

    content = HOOK_TEMPLATE.format(ledger_path=ledger_path)
    with open(hook_path, "w") as f:
        f.write(content)
    os.chmod(hook_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    return True


def git_dir_from_workspace(workspace_config_path: str) -> Optional[str]:
    """Walk up from workspace_config_path looking for a .git/ directory.

    Returns the git root (the directory containing .git/) or None.
    """
    current = os.path.dirname(workspace_config_path)
    while True:
        if os.path.isdir(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None
