"""F.11.5 — `causadb config mcp` subcommand.

Generates MCP server configuration files for various AI coding tools
(opencode, claude-code, codex-cli, cursor, windsurf, gemini-cli, aider).

Artículo II: thin wrapper over templates. No reimplemented logic.
Artículo VII: zero new dependencies (stdlib only).
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Tuple

from causadb._workspace import WorkspaceManager


_MATRIX_PATH = Path(__file__).resolve().parents[2] / "docs" / "mcp_contract_matrix.json"


def _load_contract_matrix() -> dict:
    with _MATRIX_PATH.open() as f:
        data = json.load(f)
    entries = {entry["client"]: entry for entry in data["contracts"]}
    return entries


def cmd_config_mcp(args) -> Tuple[int, str]:
    """Handle ``causadb config mcp``.

    Args namespace must have: .tool (str|None), .auto (bool),
    .project (str|None), .output (str|None).
    """
    try:
        return _cmd_config_mcp_inner(args)
    except Exception as exc:
        return (1, json.dumps({"error": f"MCP configuration failed: {exc}"}))


def _cmd_config_mcp_inner(args) -> Tuple[int, str]:
    project_path = os.path.abspath(args.project or os.getcwd())

    # Resolve workspace
    config_path = WorkspaceManager.discover(project_path)
    if config_path is None:
        return (1, json.dumps({
            "error": (
                f"No .causadb/ workspace found at {project_path}. "
                f"Run `causadb init {project_path}` first."
            ),
        }))
    ws = WorkspaceManager.load(config_path)

    # --tool or --auto must be provided
    if not args.tool and not args.auto:
        return (1, json.dumps({
            "error": "Either --tool <name> or --auto must be provided.",
        }))

    if args.tool:
        # Windsurf special-case warnings (global-only)
        if args.tool == "windsurf":
            if args.project is not None:
                print(
                    "Warning: --project is ignored for windsurf "
                    "(always writes globally)",
                    file=sys.stderr,
                )
            if args.output is not None:
                print(
                    "Warning: --output is ignored for windsurf "
                    "(always writes globally)",
                    file=sys.stderr,
                )
        return _generate_for_tool(args.tool, ws, project_path, args.output)

    # --auto mode
    return _generate_auto(ws, project_path)


def _workspace_dir_from_ledger(ws) -> str:
    """Deriva el workspace_dir (raíz del proyecto) desde el ledger_path.

    ``ws.ledger_path`` vive en ``<proyecto>/.causadb/ledger.log`` → el
    workspace_dir es el abuelo del archivo.
    """
    return os.path.dirname(os.path.dirname(ws.ledger_path))


def _resolve_mcp_launcher() -> str:
    """Resolve the installed CausaDB MCP console script, never the CLI."""
    def is_mcp_launcher(path: Path) -> bool:
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            return False
        # Installed console scripts are executable sh/python wrappers.  The
        # shell preamble must actually exec the interpreter; matching an import
        # in a comment or inert body is not sufficient.
        return (
            "causadb.mcp.server" in text
            and "from causadb.mcp.server import" in text
            and "exec" in text
        )

    candidates = []
    found = shutil.which("causadb-mcp")
    if found:
        found_path = Path(found)
        if not found_path.is_file() or not os.access(found_path, os.X_OK):
            raise RuntimeError("The `causadb-mcp` launcher found on PATH is not executable.")
        if not is_mcp_launcher(found_path):
            raise RuntimeError("The `causadb-mcp` launcher found on PATH is not the CausaDB MCP entrypoint.")
        return str(found_path)
    # A CLI launched from a checkout commonly has its sibling console script
    # in the same virtualenv, even when the caller's PATH is intentionally
    # minimal.  Derive it from this module, not an operator-specific path.
    module_root = Path(__file__).resolve().parents[2]
    candidates.extend((module_root / name / "causadb-mcp" for name in (".venv/bin", "venv/bin")))
    candidates.append(Path(sys.executable).parent / "causadb-mcp")
    for candidate in candidates:
        candidate = Path(candidate)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if is_mcp_launcher(candidate):
            return str(candidate)
    raise RuntimeError(
        "No valid executable `causadb-mcp` MCP server was found. "
        "Install CausaDB in the active environment (`python -m pip install causadb`) "
        "or put its environment's causadb-mcp entrypoint on PATH."
    )


# ---------------------------------------------------------------------------
# Templates — each returns (data_dict, resolved_output_path)
# ---------------------------------------------------------------------------

def _tmpl_opencode(ws, project_path, output_path):
    """OpenCode: write into the config file the client actually reads.

    opencode only loads ``opencode.json`` or ``opencode.jsonc`` in the project
    root. Writing a disconnected fragment name (``causadb.opencode.jsonc``)
    forces a manual merge — unacceptable for a commercial product. Default to
    ``project/opencode.json`` and merge the ``mcp`` block into any existing
    config so the user's other servers/settings are preserved.
    """
    resolved = output_path or _opencode_default_path(project_path)
    if os.path.exists(resolved) and Path(resolved).suffix.lower() == ".jsonc":
        raise ValueError(f"Existing JSONC is unsupported; migrate manually: {resolved}")
    command = _resolve_mcp_launcher()
    block = {
        "mcp": {
            "causadb": {
                "type": "local",
                "command": [command],
                "enabled": True,
                "environment": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
            },
        },
    }
    data = _merge_opencode(existing_path=resolved, block=block)
    return data, resolved


def _opencode_default_path(project_path: str) -> str:
    """The config file opencode reads in the project (json preferred, jsonc if present)."""
    jsonc = os.path.join(project_path, "opencode.jsonc")
    if os.path.exists(jsonc):
        return jsonc
    return os.path.join(project_path, "opencode.json")


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC, preserving string literals."""
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _merge_opencode(existing_path: str, block: dict) -> dict:
    """Return the merged dict for opencode config.

    If ``existing_path`` exists it is parsed (JSON or JSONC) and the ``mcp``
    block is deep-merged in, preserving unrelated keys. Otherwise a fresh
    dict wrapping ``block`` is returned.
    """
    if not os.path.exists(existing_path):
        return dict(block)

    with open(existing_path) as f:
        raw = f.read()

    if not raw.strip():
        return dict(block)

    existing = json.loads(_strip_jsonc_comments(raw))

    if not isinstance(existing, dict):
        return dict(block)

    merged_mcp = existing.get("mcp")
    if not isinstance(merged_mcp, dict):
        merged_mcp = {}
    for key, value in block["mcp"].items():
        merged_mcp[key] = value
    existing["mcp"] = merged_mcp
    return existing


def _merge_json_mcp_servers(existing_path: str, block: dict) -> dict:
    """Merge a standard ``mcpServers`` config without losing user settings.

    Only the CausaDB entry is replaced; unrelated top-level keys and other
    MCP servers remain byte-semantically represented after JSON round-trip.
    Existing JSONC is intentionally rejected by destination preflight.
    """
    if not os.path.exists(existing_path):
        return dict(block)
    with open(existing_path, encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return dict(block)
    existing = json.loads(raw)
    if not isinstance(existing, dict):
        return dict(block)
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["causadb"] = block["mcpServers"]["causadb"]
    existing["mcpServers"] = servers
    return existing


def _tmpl_claude_code(ws, project_path, output_path):
    """Claude Code: top-level key is ``mcpServers``, command as string + args list."""
    resolved = output_path or os.path.join(project_path, ".mcp.json")
    command = _resolve_mcp_launcher()
    block = {
        "mcpServers": {
            "causadb": {
                "command": command,
                "args": [],
                "env": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
            },
        },
    }
    data = _merge_json_mcp_servers(resolved, block)
    return data, resolved


def _tmpl_codex_cli(ws, project_path, output_path):
    """Codex CLI: TOML format ``[mcp_servers.causadb]``."""
    resolved = output_path or os.path.join(project_path, ".codex", "config.toml")
    command = _resolve_mcp_launcher()
    data = {
        "mcp_servers": {
            "causadb": {
                "command": command,
                "args": [],
                "enabled": True,
                "env": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
            },
        },
    }
    return data, resolved


def _tmpl_cursor(ws, project_path, output_path):
    """Cursor: ``mcpServers``, command string + args list."""
    resolved = output_path or os.path.join(project_path, ".cursor", "mcp.json")
    command = _resolve_mcp_launcher()
    block = {
        "mcpServers": {
            "causadb": {
                "command": command,
                "args": [],
                "env": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
            },
        },
    }
    data = _merge_json_mcp_servers(resolved, block)
    return data, resolved


def _tmpl_windsurf(ws, _project_path, _output_path):
    """Windsurf: always writes globally (``~/.codeium/windsurf/mcp_config.json``).

    ``_output_path`` is ignored — always uses the global location.
    """
    resolved = os.path.expanduser("~/.codeium/windsurf/mcp_config.json")
    command = _resolve_mcp_launcher()
    block = {
        "mcpServers": {
            "causadb": {
                "command": command,
                "args": [],
                "env": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
            },
        },
    }
    data = _merge_json_mcp_servers(resolved, block)
    return data, resolved


def _tmpl_gemini_cli(ws, project_path, output_path):
    """Gemini CLI: ``mcpServers`` with ``trust: true`` and env (ledger + workspace)."""
    resolved = output_path or os.path.join(project_path, ".gemini", "settings.json")
    command = _resolve_mcp_launcher()
    block = {
        "mcpServers": {
            "causadb": {
                "command": command,
                "args": [],
                "env": {
                    "CAUSADB_LEDGER_PATH": ws.ledger_path,
                    "CAUSADB_WORKSPACE_DIR": _workspace_dir_from_ledger(ws),
                },
                "trust": True,
            },
        },
    }
    data = _merge_json_mcp_servers(resolved, block)
    return data, resolved


def _tmpl_aider(ws, project_path, output_path):
    """Aider does not support MCP natively — returns None."""
    return None, None


# ---------------------------------------------------------------------------
# Tool registry: name → (template_fn, format)
# ---------------------------------------------------------------------------

_TOOL_TEMPLATES = {
    "opencode": (_tmpl_opencode, "json"),
    "claude-code": (_tmpl_claude_code, "json"),
    "codex-cli": (_tmpl_codex_cli, "toml"),
    "cursor": (_tmpl_cursor, "json"),
    "windsurf": (_tmpl_windsurf, "json"),
    "gemini-cli": (_tmpl_gemini_cli, "json"),
    "aider": (_tmpl_aider, "message"),
}


def _validate_registry() -> None:
    """Fail closed if a template drifts from the closed contract registry."""
    matrix = _load_contract_matrix()
    for tool, (_, fmt) in _TOOL_TEMPLATES.items():
        entry = matrix.get(tool)
        if not entry or entry["status"] not in {"confirmed", "negative"} or entry["format"] != fmt:
            raise RuntimeError(f"MCP template registry drift for {tool}")


def _generate_for_tool(tool_name: str, ws, project_path: str,
                       output_path: str = None) -> Tuple[int, str]:
    """Generate config for a single tool. Returns (exit_code, output_json)."""
    try:
        _validate_registry()
        if tool_name not in _TOOL_TEMPLATES:
            return (1, json.dumps({"error": f"Unknown tool: {tool_name}"}))
        tmpl_func, fmt = _TOOL_TEMPLATES[tool_name]
        if tool_name == "aider":
            return (0, json.dumps({
                "tool": "aider",
                "message": (
                    "Aider does not support MCP servers natively. "
                    "Use the CausaDB CLI directly: causadb <command>"
                ),
            }, sort_keys=True))
        data, resolved_path = tmpl_func(ws, project_path, output_path)
        _validate_destination(resolved_path, fmt)
        _write_output(resolved_path, data, fmt)
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        return (1, json.dumps({"error": str(exc)}))

    return (0, json.dumps({
        "tool": tool_name,
        "output_path": resolved_path,
        "ledger_path": ws.ledger_path,
    }, sort_keys=True))


def _generate_auto(ws, project_path: str) -> Tuple[int, str]:
    """Detect installed tools and generate configs for each."""
    detected = _detect_installed_tools()
    try:
        _resolve_mcp_launcher()
    except RuntimeError as exc:
        return (1, json.dumps({"error": str(exc)}))
    configured, outputs, pending = [], {}, []
    try:
        _validate_registry()
        # Complete preflight: templates, parsing, launcher and destinations all
        # succeed before the first byte is written.
        for tool_name in detected:
            if tool_name == "aider" or tool_name not in _TOOL_TEMPLATES:
                continue
            tmpl_func, fmt = _TOOL_TEMPLATES[tool_name]
            data, resolved_path = tmpl_func(ws, project_path, None)
            _validate_destination(resolved_path, fmt)
            pending.append((tool_name, resolved_path, data, fmt))
        _preflight_destination_ancestors(pending)
        _commit_outputs_transactionally(pending)
        for tool_name, resolved_path, _data, _fmt in pending:
            configured.append(tool_name)
            outputs[tool_name] = resolved_path
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        return (1, json.dumps({"error": str(exc)}))

    return (0, json.dumps({
        "configured": configured,
        "outputs": outputs,
        "ledger_path": ws.ledger_path,
    }, sort_keys=True))


def _preflight_destination_ancestors(pending) -> None:
    """Check every ancestor without creating anything."""
    checked = set()
    for _tool, path, _data, _fmt in pending:
        current = Path(os.path.abspath(path)).parent
        while True:
            name = str(current)
            if name in checked:
                break
            checked.add(name)
            if current.exists():
                if not current.is_dir():
                    raise OSError(f"Destination parent is not a directory: {current}")
                if not os.access(current, os.W_OK | os.X_OK):
                    raise OSError(f"Destination directory is not writable: {current}")
                break
            parent = current.parent
            if parent == current:
                break
            current = parent


def _commit_outputs_transactionally(pending) -> None:
    """Stage all files, then replace destinations with rollback on failure."""
    staged = []
    backups = []
    replaced = []
    created_dirs = []
    committed = False
    try:
        for _tool, path, data, fmt in pending:
            parent = os.path.dirname(os.path.abspath(path)) or "."
            if not os.path.exists(parent):
                missing = []
                cursor = Path(parent)
                while not cursor.exists():
                    missing.append(cursor)
                    cursor = cursor.parent
                os.makedirs(parent, exist_ok=True)
                created_dirs.extend(missing)
            fd, temporary = tempfile.mkstemp(
                prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=parent)
            with os.fdopen(fd, "w") as f:
                if fmt == "json":
                    json.dump(data, f, indent=2)
                    f.write("\n")
                elif fmt == "toml":
                    _write_toml_file(f, data)
                f.flush()
                os.fsync(f.fileno())
            staged.append((temporary, path))

        for _temporary, path in staged:
            if os.path.exists(path):
                fd, backup = tempfile.mkstemp(
                    prefix="." + os.path.basename(path) + ".", suffix=".bak",
                    dir=os.path.dirname(os.path.abspath(path)))
                os.close(fd)
                shutil.copyfile(path, backup)
                backups.append((path, backup))
            os.replace(_temporary, path)
            replaced.append(path)
        committed = True
    except Exception:
        for path in reversed(replaced):
            backup = next((b for p, b in backups if p == path), None)
            try:
                if backup:
                    os.replace(backup, path)
                else:
                    os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        for temporary, _path in staged:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        for _path, backup in backups:
            try:
                os.unlink(backup)
            except FileNotFoundError:
                pass
        if not committed:
            for directory in sorted(created_dirs, key=lambda p: len(str(p)), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass


def _write_output(path: str, data: Any, fmt: str):
    """Write data to file in JSON or TOML format."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w") as f:
            if fmt == "json":
                json.dump(data, f, indent=2)
                f.write("\n")
            elif fmt == "toml":
                _write_toml_file(f, data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _validate_destination(path: str, fmt: str) -> None:
    """Validate existing formats without ever changing their bytes."""
    if not path:
        return
    if os.path.exists(path):
        if not os.path.isfile(path) or not os.access(path, os.W_OK):
            raise OSError(f"Destination is not writable: {path}")
        if fmt == "toml":
            raise ValueError(f"Existing TOML is unsupported; migrate manually: {path}")
        if Path(path).suffix.lower() == ".jsonc":
            raise ValueError(f"Existing JSONC is unsupported; migrate manually: {path}")
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        if raw.strip():
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"Existing JSON must contain an object: {path}")
    else:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        if os.path.exists(parent) and not os.access(parent, os.W_OK):
            raise OSError(f"Destination directory is not writable: {parent}")


# ---------------------------------------------------------------------------
# Minimal inline TOML serializer (stdlib only — Article VII)
# ---------------------------------------------------------------------------

def _write_toml(path: str, data: dict):
    """Write a minimal TOML file.

    Handles the specific structure we need:
        [section.subsection]
        key = "string"
        key = true
        key = ["a", "b"]
        key = { inline = "table" }
    """
    with open(path, "w") as f:
        _write_toml_file(f, data)


def _write_toml_file(f, data: dict):
    for section, entries in data.items():
            if isinstance(entries, dict):
                # Check if entries have nested dicts (multi-level sections)
                for sub_key, sub_value in entries.items():
                    f.write(f"[{section}.{sub_key}]\n")
                    _write_toml_entries(f, sub_value)
    f.flush()


def _write_toml_entries(f, entries: dict):
    """Write key-value pairs with 2-space indent."""
    for key, value in entries.items():
        if isinstance(value, str):
            f.write(f"  {key} = \"{value}\"\n")
        elif isinstance(value, bool):
            f.write(f"  {key} = {'true' if value else 'false'}\n")
        elif isinstance(value, list):
            items = ", ".join(f'"{v}"' for v in value)
            f.write(f"  {key} = [{items}]\n")
        elif isinstance(value, dict):
            pairs = []
            for k, v in value.items():
                if isinstance(v, bool):
                    pairs.append(f"{k} = {'true' if v else 'false'}")
                else:
                    pairs.append(f'{k} = "{v}"')
            f.write(f"  {key} = {{ {', '.join(pairs)} }}\n")


# ---------------------------------------------------------------------------
# Tool detection (uses shutil.which + file-checks)
# ---------------------------------------------------------------------------

def _detect_installed_tools() -> list:
    """Detect which AI coding tools are installed on this system."""
    tools = []

    # opencode: which opencode, or config file exists
    if shutil.which("opencode") or os.path.exists(
        os.path.expanduser("~/.config/opencode/opencode.jsonc"),
    ):
        tools.append("opencode")

    if shutil.which("claude"):
        tools.append("claude-code")
    if shutil.which("codex"):
        tools.append("codex-cli")
    if shutil.which("cursor"):
        tools.append("cursor")
    if os.path.exists(os.path.expanduser("~/.codeium/windsurf/mcp_config.json")):
        tools.append("windsurf")
    if shutil.which("gemini"):
        tools.append("gemini-cli")
    if shutil.which("aider"):
        tools.append("aider")

    return tools
