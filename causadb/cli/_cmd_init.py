"""`causadb init` subcommand — thin wrapper around WorkspaceManager.init.

Artículo II: thin wrapper. Artículo III: test-first.
"""
import json
import os
import re
import sys
from typing import Tuple, Optional
import shutil
from pathlib import Path

from causadb._workspace import WorkspaceManager, CausaDBWorkspace
from causadb._git_hook import install_post_commit_hook, git_dir_from_workspace
from causadb._shell_hook import install as install_shell_hook
from causadb._telemetry import set_enabled as _set_telemetry

MARKER_OPEN = "<!-- CAUSADB-GAP5 -->"
MARKER_CLOSE = "<!-- CAUSADB-GAP5 --><!-- end -->"
# Bloque completo incluyendo el salto de línea inicial que lo separa del
# contenido previo del archivo.
_BLOCK_RE = re.compile(
    r"\n" + re.escape(MARKER_OPEN) + r".*?" + re.escape(MARKER_CLOSE),
    re.DOTALL,
)


def _agent_rules_map(home: Optional[Path] = None) -> dict:
    """Mapeo agente → archivo de reglas. Central para detect/seed."""
    home = home or Path.home()
    return {
        "opencode": home / ".config" / "opencode" / "AGENTS.md",
        "gemini":   home / ".gemini" / "GEMINI.md",
        "claude":   home / ".claude" / "CLAUDE.md",
        "codex":    home / ".codex" / "instructions.md",
        # Cursor user rules are stored under ~/.cursor/rules. The .mdc
        # extension is accepted by Cursor's rule loader.
        "cursor":   home / ".cursor" / "rules" / "causadb.mdc",
        "grok":     home / ".grok" / "AGENTS.md",
        "hermes":   home / ".hermes" / "AGENTS.md",
        "openjarvis": home / ".openjarvis" / "AGENTS.md",
    }


def detect_agent(explicit: Optional[str] = None) -> Optional[str]:
    """Detecta qué agente está tirando este comando, para saber a qué
    archivo de reglas sembrar la 1 línea de doctrina.

    Precedencia (deuda #23 — entornos multi-agente):
      1. *explicit* — flag ``--agent`` del CLI (override directo del
         operador; gana siempre).
      2. ``CAUSADB_AGENT`` env var (para tools que no exponen flags, o
         cuando el operador no quiere/debe pasar el flag en cada llamada).
      3. Archivo de reglas existente, en orden hardcodeado
         ``[opencode, gemini, claude, codex]``. Heurística: el primer
         ``AGENTS.md``/``GEMINI.md``/... que exista gana — en esta máquina
         ``AGENTS.md`` existe y por lo tanto siempre gana sobre gemini.
         **Por eso el flag/env son los únicos caminos robustos en
         multi-agente.**
      4. Binario en PATH (mismo orden hardcodeado).

    ``explicit`` se valida contra el mapa de agentes conocidos; si no es
    uno de ellos se ignora (se cae al siguiente nivel) en vez de crashear,
    para no romper el flujo del operador por un typo.
    """
    home = Path.home()
    AGENT_RULES = _agent_rules_map(home)

    if explicit and explicit in AGENT_RULES:
        return explicit

    env_agent = os.environ.get("CAUSADB_AGENT")
    if env_agent and env_agent in AGENT_RULES:
        return env_agent

    if (home / ".config" / "opencode" / "AGENTS.md").exists():
        return "opencode"
    if (home / ".gemini" / "GEMINI.md").exists():
        return "gemini"
    if (home / ".claude" / "CLAUDE.md").exists():
        return "claude"
    if (home / ".codex" / "instructions.md").exists():
        return "codex"

    if shutil.which("opencode"):
        return "opencode"
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    if shutil.which("gemini"):
        return "gemini"
    if shutil.which("aider"):
        return "aider"

    return None


def seed_doctrina_link(agent: str, ledger_path: str) -> Optional[str]:
    """Sembrar/actualizar el puntero al canon en el archivo de reglas del
    agente detectado, seteando ledger_path.

    Doctrina (BIT-49 / briefing:92): el canon vive DENTRO del producto y se
    mantiene una sola vez. El archivo de reglas de cada tool lleva un puntero
    corto con: (a) el ledger_path del workspace, (b) los casos de uso (para
    que el agente sepa cuándo leerlo), (c) cómo leer el canon (`causadb canon`
    / resource MCP causadb://canon). NUNCA URLs externas (el repo de GitHub
    no existe todavía) ni rutas absolutas de máquina.

    Idempotente (Art. VII): si el marker ya existe se REEMPLAZA el bloque
    con el nuevo ``ledger_path`` — nunca deja un valor stale de una sesión
    previa apuntando a un path muerto (WARN-1 G5.B).
    """
    home = Path.home()
    AGENT_RULES = _agent_rules_map(home)
    if agent not in AGENT_RULES:
        return None

    rules_path = AGENT_RULES[agent]
    rules_path.parent.mkdir(parents=True, exist_ok=True)

    block = (
        f"\n{MARKER_OPEN}\n"
        f"CausaDB ledger_path={ledger_path}\n"
        f"Este proyecto usa CausaDB (ledger + Chronicle) como memoria del agente.\n"
        f"Para auditar qué se hizo, ahorrar tokens (evitar excavar a mano), ver\n"
        f"decisiones y trazabilidad, reconstruir estado, o recuperar archivos mal\n"
        f"mutados: leé la guía del agente con `causadb canon` (o el resource MCP\n"
        f"causadb://canon). Leela solo cuando necesites alguno de esos casos.\n"
        f"REGLA 1 — al cerrar sesión (o al pausar con pendiente), logueá una\n"
        f"GOVERNANCE_DECISION (tactical/low/agent) con qué se hizo y qué queda\n"
        f"pendiente, para que la próxima sesión recupere contexto barato.\n"
        f"{MARKER_CLOSE}\n"
    )

    if rules_path.exists():
        content = rules_path.read_text()
        if _BLOCK_RE.search(content):
            content = _BLOCK_RE.sub(block, content, count=1)
        else:
            content = content + block
        rules_path.write_text(content)
    else:
        rules_path.write_text(block)

    return str(rules_path)


_AGENT_TO_MCP_TOOL = {
    "opencode": "opencode",
    "gemini": "gemini-cli",
    "claude": "claude-code",
    "codex": "codex-cli",
    "cursor": "cursor",
    "windsurf": "windsurf",
    "aider": "aider",
}


def configure_mcp_for_agent(agent: Optional[str], ws) -> Optional[dict]:
    """Configure MCP only for an explicitly selected agent.

    Reuses the canonical MCP templates and writes beside the workspace, even
    when init was invoked from a child directory discovered by WorkspaceManager.
    """
    if not agent:
        return None

    tool = _AGENT_TO_MCP_TOOL.get(agent)
    if tool is None:
        return None

    from causadb.cli._cmd_config_mcp import (
        _generate_for_tool,
        _workspace_dir_from_ledger,
    )

    project_path = _workspace_dir_from_ledger(ws)
    exit_code, output = _generate_for_tool(tool, ws, project_path)
    if exit_code != 0:
        raise ValueError(json.loads(output).get("error", output))
    result = json.loads(output)
    result["mcp_tool"] = tool
    return result


def cmd_init(args) -> Tuple[int, str]:
    """Delegate to ``WorkspaceManager.init(workspace_path)``.

    If ``workspace`` is None, defaults to ``os.getcwd()``.

    If --git-hooks is set, also installs a post-commit hook for automatic
    COMMIT_MADE logging (F.11.3).

    If --with-hook is set, also installs the shell hook for automatic
    command capture (ctx_id = ``init:<project_name>``).

    If --telemetry-enabled is provided, uses that value; otherwise prompts
    the user interactively (default Y).

    Returns (exit_code, json_string). On ValueError/FileExistsError, emits a
    Fall-Closed JSON error object with exit code 1.
    """
    workspace_path = args.workspace or os.getcwd()
    workspace_path = os.path.abspath(workspace_path)

    found = WorkspaceManager.discover(workspace_path)

    if found:
        answer = "y"
        no_seed = getattr(args, "no_seed_doctrina", False)
        explicit = getattr(args, "agent", None)
        agent = None if no_seed else detect_agent(explicit)
        if sys.stdin.isatty():
            try:
                seed_notice = ""
                if agent:
                    rules_map = _agent_rules_map()
                    rules_path = rules_map.get(agent)
                    seed_notice = (
                        f"Se escribirá una línea de doctrina en "
                        f"{rules_path if rules_path else agent}."
                    )
                answer = input(
                    f"Se encontró workspace existente en {found}.\n"
                    f"¿Conectar a este workspace? [Y/n]: "
                ).strip().lower() or "y"
                if answer in ("y", ""):
                    if seed_notice:
                        print(f"→ {seed_notice}", file=sys.stderr)
            except (EOFError, KeyboardInterrupt):
                answer = "y"

        if answer in ("n", "no"):
            return (1, json.dumps({
                "error": "init abortado",
                "message": "Para crear en otra ruta: causadb init /ruta/absoluta",
                "found_workspace": found,
            }))

        ws = WorkspaceManager.load(found)
        sedd = seed_doctrina_link(agent, ws.ledger_path) if agent else None
        mcp = configure_mcp_for_agent(explicit, ws)

        # WARN-2: flags de "crear" no aplican en modo conectar — avisar.
        warnings = []
        for flag_name, label in (
            ("git_hooks", "--git-hooks"),
            ("with_hook", "--with-hook"),
            ("with_assistant", "--with-assistant"),
        ):
            if getattr(args, flag_name, False):
                warnings.append(f"{label} ignorado: ya existe un workspace, no se crea")

        return (0, json.dumps({
            "connected": True,
            "config_path": found,
            "ledger_path": ws.ledger_path,
            "seed_doctrina_to_agent": agent,
            "seed_doctrina_to_file": sedd,
            "warnings": warnings,
            **({"mcp_tool": mcp["mcp_tool"], "mcp_output_path": mcp.get("output_path")}
               if mcp else {}),
        }, sort_keys=True))
    
    try:
        result = WorkspaceManager.init(workspace_path)
        output = dict(result)

        # Determine telemetry preference
        telemetry_enabled = True  # default
        explicit_flag = getattr(args, "telemetry_enabled", None)
        if explicit_flag is not None:
            telemetry_enabled = explicit_flag
        else:
            # Interactive prompt
            if sys.stdin.isatty():
                try:
                    answer = input("¿Permitir telemetría anónima? [Y/n]: ").strip().lower()
                    if answer in ("n", "no"):
                        telemetry_enabled = False
                except (EOFError, KeyboardInterrupt):
                    telemetry_enabled = True

        # Persist telemetry preference to user-level config
        from causadb._telemetry import set_enabled
        set_enabled(telemetry_enabled)
        output["telemetry_enabled"] = telemetry_enabled

        ws = WorkspaceManager.load(result["config_path"])
        mcp = configure_mcp_for_agent(getattr(args, "agent", None), ws)
        if mcp:
            output["mcp_tool"] = mcp["mcp_tool"]
            if "output_path" in mcp:
                output["mcp_output_path"] = mcp["output_path"]

        if getattr(args, "git_hooks", False):
            config_path = result["config_path"]
            ws = WorkspaceManager.load(config_path)
            git_root = git_dir_from_workspace(config_path)
            if git_root is not None:
                installed = install_post_commit_hook(git_root, ws.ledger_path)
                output["git_hook_installed"] = installed
                if installed:
                    output["git_hook_path"] = os.path.join(
                        git_root, ".git", "hooks", "post-commit"
                    )
            else:
                output["git_hook_installed"] = False
                output["git_hook_error"] = "No .git/ directory found"

        if getattr(args, "with_hook", False):
            project_name = os.path.basename(os.path.abspath(workspace_path))
            hook_installed = install_shell_hook(ctx_id=f"init_{project_name}")
            output["shell_hook_installed"] = hook_installed

        if getattr(args, "with_assistant", False):
            from causadb._assistant import Assistant
            print('  Downloading assistant model (SmolLM2, ~130MB)...')
            if Assistant.pull_model():
                print('  ✓ Assistant model downloaded.')
                output["assistant_configured"] = True
            else:
                print('  ⚠ Could not download model. Make sure Ollama is running on port 11434.')
                output["assistant_configured"] = False

        return (0, json.dumps(output, sort_keys=True))
    except (ValueError, FileExistsError) as e:
        return (1, json.dumps({"error": str(e), "error_type": type(e).__name__}))
