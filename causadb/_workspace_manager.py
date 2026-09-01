"""I.1 — Multi-workspace manager: isolated ledgers per named workspace.

Each workspace lives in its own directory under ``root_dir``:

    {root_dir}/{name}/.causadb/ledger.log
    {root_dir}/{name}/.causadb/config.json
    {root_dir}/{name}/.causadb/CAUSADB_CHRONICLE.md

A ``.current`` file at the root_dir level tracks the active workspace.

Artículo I: Ledger is only written via ``causadb_init`` → ``LedgerWriter``.
Artículo II: Thin wrapper — no logic reimplemented.
Artículo III: Test-first (see tests/test_multi_workspace.py).
"""

import datetime
import json
import os
import shutil
import uuid


class WorkspaceManager:
    """Manages multiple named workspaces with isolated ledgers.

    Default ``root_dir`` is ``~/.causadb/workspaces/``.
    A ``default`` workspace is auto-created on init if it doesn't exist.
    """

    def __init__(self, root_dir=None):
        if root_dir is None:
            root_dir = os.path.expanduser("~/.causadb/workspaces")
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)
        if not self.exists("default"):
            self.create("default")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _workspace_dir(self, name: str) -> str:
        return os.path.join(self.root_dir, name)

    def _causadb_dir(self, name: str) -> str:
        return os.path.join(self._workspace_dir(name), ".causadb")

    def _ledger_path(self, name: str) -> str:
        return os.path.join(self._causadb_dir(name), "ledger.log")

    def _config_path(self, name: str) -> str:
        return os.path.join(self._causadb_dir(name), "config.json")

    def _current_file(self) -> str:
        return os.path.join(self.root_dir, ".current")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str):
        """Create a new workspace with its own isolated ledger.

        Delegates ledger creation to ``causadb_init`` (Artículo I).
        Creates a ``config.json`` with workspace metadata.
        Switches to this workspace if no current workspace exists.
        """
        if self.exists(name):
            raise FileExistsError(f"Workspace already exists: {name}")

        ws_dir = self._workspace_dir(name)
        os.makedirs(ws_dir, exist_ok=True)

        # Initialize .causadb/ with ledger + chronicle via causadb_init
        from causadb._init import causadb_init

        cd = self._causadb_dir(name)
        init_result = causadb_init(cd)

        # Write workspace metadata config (separate from CausaDBWorkspace config)
        config = {
            "name": name,
            "ledger_path": init_result["ledger_path"],
            "created_at": datetime.datetime.now().isoformat(),
            "project_id": str(uuid.uuid4()),
        }
        with open(self._config_path(name), "w") as f:
            json.dump(config, f, indent=2, sort_keys=True)

        # Register as last workspace (best-effort)
        try:
            from causadb._workspace import record_last_workspace
            record_last_workspace(init_result["ledger_path"])
        except Exception:
            pass

        # Auto-switch if this is the first workspace
        if self.current() is None:
            self.switch(name)

    def list(self) -> list[str]:
        """Return sorted list of workspace names.

        Scans ``root_dir`` for subdirectories that contain a valid
        ``.causadb/config.json``.
        """
        if not os.path.isdir(self.root_dir):
            return []
        workspaces = []
        for entry in sorted(os.listdir(self.root_dir)):
            if entry.startswith("."):
                continue
            entry_path = os.path.join(self.root_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            config_file = os.path.join(entry_path, ".causadb", "config.json")
            if os.path.isfile(config_file):
                workspaces.append(entry)
        return workspaces

    def delete(self, name: str):
        """Delete a workspace and all its data (ledger, config, chronicle).

        Clears the ``.current`` pointer if the deleted workspace was active.
        """
        if not self.exists(name):
            raise FileNotFoundError(f"Workspace not found: {name}")
        shutil.rmtree(self._workspace_dir(name))
        if self.current() == name:
            current_file = self._current_file()
            if os.path.exists(current_file):
                os.remove(current_file)

    def switch(self, name: str):
        """Switch the active workspace pointer.

        Writes the workspace name into the ``.current`` file and records
        the workspace ledger as the last workspace used.
        """
        if not self.exists(name):
            raise FileNotFoundError(f"Workspace not found: {name}")
        current_file = self._current_file()
        with open(current_file, "w") as f:
            f.write(name + "\n")
        try:
            from causadb._workspace import record_last_workspace
            record_last_workspace(self.ledger_path(name))
        except Exception:
            pass

    def current(self) -> str | None:
        """Return the name of the active workspace, or ``None``."""
        current_file = self._current_file()
        if os.path.exists(current_file):
            with open(current_file) as f:
                name = f.read().strip()
                if name and self.exists(name):
                    return name
        return None

    def ledger_path(self, name: str) -> str:
        """Return the absolute path to a workspace's ledger file."""
        if not self.exists(name):
            raise FileNotFoundError(f"Workspace not found: {name}")
        return self._ledger_path(name)

    def exists(self, name: str) -> bool:
        """Check if a workspace (with config.json) exists."""
        config_file = self._config_path(name)
        return os.path.isfile(config_file)
