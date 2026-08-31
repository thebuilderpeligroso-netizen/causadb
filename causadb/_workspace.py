"""F.11.2 — Workspace discovery, config persistence, and init.

Artículo I: The ledger is only written via LedgerWriter.append() in
causadb_init(). This module reads/writes config.json and walks the
filesystem — it never opens ledger.log directly.

Artículo II: Thin wrapper over causadb_init() for workspace creation.
"""

import datetime
import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from causadb._init import causadb_init


class NoWorkspaceError(FileNotFoundError):
    """Raised when no .causadb workspace is found and no --ledger given.

    If ``fallback_last`` was requested but no usable last workspace exists,
    the message suggests how to register one.
    """

    def __init__(self, tried_last: bool = False):
        hint = (
            "Run `causadb init <path>`, provide --ledger explicitly, "
            "or `causadb revive --last` (if a previous workspace exists)."
            if tried_last
            else "Run `causadb init <path>` or provide --ledger explicitly."
        )
        super().__init__(
            "No .causadb/ workspace found and no --ledger provided. " + hint
        )


CAUSADB_DIR = ".causadb"
CONFIG_FILE = "config.json"
LAST_WORKSPACE_FILE = "~/.causadb/last_workspace.json"


def _last_workspace_path(registry_path: Optional[str] = None) -> str:
    """Return the last-workspace registry file path.

    Precedence: explicit arg > ``CAUSADB_LAST_WORKSPACE`` env var (used by
    tests to isolate the real ``~/.causadb/``) > ``~/.causadb/last_workspace.json``.
    """
    if registry_path:
        return os.path.abspath(registry_path)
    env_path = os.environ.get("CAUSADB_LAST_WORKSPACE")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.expanduser(LAST_WORKSPACE_FILE)


def record_last_workspace(ledger_path: str, registry_path: Optional[str] = None) -> None:
    """Persist the last used ledger path (atomic write + fsync).

    Only records ledgers that exist on disk (Artículo IX anti-teatro: a
    registry pointing to a nonexistent ledger is useless). The file lives in
    ``~/.causadb/last_workspace.json`` and is read by ``causadb revive --last``
    and as a fallback in workspace discovery.

    Args:
        ledger_path: Absolute path to the ledger file just used.
        registry_path: Override for the registry file (tests).
    """
    ledger_path = os.path.abspath(ledger_path)
    if not os.path.isfile(ledger_path):
        return
    path = _last_workspace_path(registry_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "ledger_path": ledger_path,
        "timestamp": datetime.datetime.now().isoformat(),
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def get_last_workspace(registry_path: Optional[str] = None) -> Optional[str]:
    """Return the last recorded ledger path, or ``None``.

    Returns ``None`` when the registry file is missing, unparseable, or
    points to a ledger that no longer exists on disk (stale entry).
    """
    path = _last_workspace_path(registry_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        ledger_path = data.get("ledger_path")
    except (ValueError, OSError, TypeError):
        return None
    if not ledger_path or not os.path.isfile(ledger_path):
        return None
    return os.path.abspath(ledger_path)


def _maybe_record(ledger_path: Optional[str]) -> None:
    """Record a resolved ledger path if it exists (best-effort)."""
    if not ledger_path:
        return
    try:
        record_last_workspace(ledger_path)
    except OSError:
        pass


def resolve_ledger(
    ledger_from_args: Optional[str] = None,
    fallback_last: bool = False,
) -> str:
    """Resolve ledger path: explicit arg > workspace discovery > last workspace > error.

    Always returns an absolute path. Converts relative paths via
    ``os.path.abspath`` so downstream consumers (LedgerWriter, etc.)
    never receive a relative path (Artículo I).

    Every successfully resolved ledger is recorded as the "last workspace"
    so a future ``causadb revive --last`` (or discovery fallback) can find it.

    Args:
        ledger_from_args: Value of ``--ledger``, if given.
        fallback_last: If discovery fails, fall back to ``get_last_workspace()``.

    Use this in CLI command handlers to support --ledger being optional.
    """
    if ledger_from_args:
        path = os.path.abspath(ledger_from_args)
        _maybe_record(path)
        return path
    config_path = WorkspaceManager.discover(os.getcwd())
    if config_path is not None:
        ws = WorkspaceManager.load(config_path)
        _maybe_record(ws.ledger_path)
        return ws.ledger_path
    if fallback_last:
        last = get_last_workspace()
        if last:
            _maybe_record(last)
            return last
        raise NoWorkspaceError(tried_last=True)
    raise NoWorkspaceError()


@dataclass
class CausaDBWorkspace:
    ledger_path: str
    watch_dirs: list[str] = field(default_factory=list)
    chronicle_path: str = ""
    daemon_enabled: bool = False
    ocb_base_path: Optional[str] = None
    blob_store_enabled: bool = True
    custom_event_types: dict = field(default_factory=dict)

    def __post_init__(self):
        if not os.path.isabs(self.ledger_path):
            raise ValueError(f"ledger_path must be an absolute path: {self.ledger_path}")
        if not self.chronicle_path:
            self.chronicle_path = os.path.join(
                os.path.dirname(self.ledger_path), "CAUSADB_CHRONICLE.md"
            )
        if self.ocb_base_path is None:
            self.ocb_base_path = os.path.join(os.path.dirname(self.ledger_path), "ocb")


class WorkspaceManager:
    """Creates, discovers, loads, and persists CausaDB workspace configurations."""

    @staticmethod
    def _causadb_dir(project_path: str) -> str:
        return os.path.join(project_path, CAUSADB_DIR)

    @staticmethod
    def _config_path(project_path: str) -> str:
        return os.path.join(project_path, CAUSADB_DIR, CONFIG_FILE)

    @staticmethod
    def discover(start_path: str = ".") -> Optional[str]:
        """Walk up from start_path looking for a valid .causadb/config.json.

        A valid workspace config MUST contain ``ledger_path`` (required by
        ``CausaDBWorkspace``). Configs without it (e.g. the global telemetry
        config ``~/.causadb/config.json``) are skipped so ``discover()``
        never returns a candidate that ``WorkspaceManager.load()`` cannot
        construct (G5.B — BIT-CHR.55).

        Returns the absolute path to config.json if found, None otherwise.
        """
        current = os.path.abspath(start_path)
        while True:
            candidate = os.path.join(current, CAUSADB_DIR, CONFIG_FILE)
            if os.path.isfile(candidate):
                try:
                    with open(candidate) as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError):
                    data = {}
                if isinstance(data, dict) and data.get("ledger_path"):
                    return candidate
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None

    @staticmethod
    def load(config_path: str) -> CausaDBWorkspace:
        """Load and validate a workspace config from its JSON file."""
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Config not found: {config_path}")
        with open(config_path) as f:
            data = json.load(f)
        from causadb._event_registry import load_from_config
        load_from_config(config_path)
        known = {f.name for f in fields(CausaDBWorkspace)}
        return CausaDBWorkspace(**{k: v for k, v in data.items() if k in known})

    @staticmethod
    def save(workspace: CausaDBWorkspace, config_path: str):
        """Persist workspace config with fsync."""
        tmp_path = config_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(asdict(workspace), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)

    @staticmethod
    def init(
        project_path: str,
        watch_dirs: Optional[list[str]] = None,
        blob_store_enabled: Optional[bool] = None,
    ) -> dict:
        """Create .causadb/ inside project_path with config.json and ledger.

        Delegates ledger creation to causadb_init() (Artículo I).
        Returns a dict with {config_path, ledger_path, chronicle_path}.
        """
        project_path = os.path.abspath(project_path)
        if os.path.exists(project_path) and not os.path.isdir(project_path):
            raise ValueError(f"Path exists and is not a directory: {project_path}")

        os.makedirs(project_path, exist_ok=True)

        cd = WorkspaceManager._causadb_dir(project_path)
        # causadb_init creates the directory + ledger + chronicle
        init_result = causadb_init(cd)

        ledger_path = init_result["ledger_path"]
        chronicle_path = init_result["chronicle_path"]

        ws_kwargs = {
            "ledger_path": ledger_path,
            "watch_dirs": watch_dirs or [project_path],
            "chronicle_path": chronicle_path,
        }
        if blob_store_enabled is not None:
            ws_kwargs["blob_store_enabled"] = blob_store_enabled
        ws = CausaDBWorkspace(**ws_kwargs)

        config_path = WorkspaceManager._config_path(project_path)
        WorkspaceManager.save(ws, config_path)

        record_last_workspace(ledger_path)

        # Crear anotadores de coordinación multi-agente
        from causadb._shared_docs import ensure_shared_docs
        ensure_shared_docs(ledger_path)

        return {
            "config_path": config_path,
            "ledger_path": ledger_path,
            "chronicle_path": chronicle_path,
        }
