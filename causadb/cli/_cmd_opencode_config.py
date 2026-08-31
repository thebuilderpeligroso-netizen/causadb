"""F.11.5 — `causadb opencode-config` subcommand (legacy alias).

Thin wrapper over ``cmd_config_mcp(tool="opencode")``.
Artículo II: no logic reimplemented here.
"""

from typing import Tuple


def cmd_opencode_config(args) -> Tuple[int, str]:
    """Generate an OpenCode MCP server config (delegates to cmd_config_mcp).

    Returns (exit_code, output_json).
    """
    from ._cmd_config_mcp import cmd_config_mcp
    args.tool = "opencode"
    args.auto = False
    return cmd_config_mcp(args)
