"""`causadb` CLI entrypoint — argparse-based, delegates to `_cmd_*` modules.

Pattern A: `main(args=None) -> int` returns the exit code and prints the JSON
output to stdout. Tests call `main(args=[...])` and capture stdout via
`capsys`. The `_cmd_*` functions return `Tuple[int, str]` (exit_code, output)
and this module is the single place that calls `print()`.

Exit codes:
  0 = success
  1 = validation/runtime error (Fall-Closed)
  2 = usage error (handled by argparse)
"""
import argparse
import json
import sys

from causadb.cli._cmd_setup import cmd_setup  # F.1 setup
from causadb.cli._cmd_init import cmd_init
from causadb.cli._cmd_genesis import cmd_genesis  # F1.1 Génesis
from causadb.cli._cmd_canon import cmd_canon
from causadb.cli._cmd_chronicle import cmd_chronicle  # F.2 chronicle
from causadb.cli._cmd_log import cmd_log
from causadb.cli._cmd_replay import cmd_replay
from causadb.cli._cmd_sentinel import cmd_sentinel
from causadb.cli._cmd_validate import cmd_validate
from causadb.cli._cmd_query import cmd_query
from causadb.cli._cmd_vigilante import cmd_vigilante
from causadb.cli._cmd_proxy import cmd_proxy, cmd_proxy_server
from causadb.cli._cmd_feedback import cmd_feedback
from causadb.cli._cmd_sandbox import cmd_sandbox
from causadb.cli._cmd_stream import cmd_stream
from causadb.cli._cmd_export import cmd_export
from causadb.cli._cmd_import import cmd_import
from causadb.cli._cmd_compliance import cmd_compliance
from causadb.cli._cmd_incident import cmd_incident
from causadb.cli._cmd_audit_trail import cmd_audit_trail
from causadb.cli._cmd_mcp_proxy import cmd_mcp_proxy
from causadb.cli._cmd_ocb import cmd_ocb
from causadb.cli._cmd_config import cmd_config
from causadb.cli._cmd_watch import cmd_watch
from causadb.cli._cmd_opencode_config import cmd_opencode_config
from causadb.cli._cmd_audit import cmd_audit
from causadb._updater import _version_gt
from causadb.cli._cmd_serve import cmd_serve
from causadb.cli._cmd_harvest import cmd_harvest  # BIT-CHR.41
from causadb.cli._cmd_restart import cmd_restart  # H-OPS.1 Fase 3
from causadb.cli._cmd_recover import cmd_recover  # recovery (Chronicle; docs/design_index.md)
from causadb.cli._cmd_impact import cmd_impact  # F.12.4 impact
from causadb.cli._cmd_why import cmd_why  # F.12.2 why
from causadb.cli._cmd_bisect import cmd_bisect  # F.12.5 bisect
from causadb.cli._cmd_trace import cmd_trace  # F.12.3 trace
from causadb.cli._cmd_resume import cmd_resume  # R.1 resume
from causadb.cli._cmd_score import cmd_score  # F.13.3.4 score
from causadb.cli._cmd_shell_hook import cmd_shell_hook  # shell-hook install|remove|status|flush
from causadb.cli._cmd_snapshot import cmd_snapshot  # R.3.4 snapshot
from causadb.cli._cmd_revive import cmd_revive  # R.2 revive
from causadb.cli._cmd_daemon import cmd_daemon  # D.1 systemd service
from causadb.cli._cmd_update import cmd_update  # Ola 2A auto-update
from causadb.cli._cmd_crash import cmd_crash  # Crash reporter
from causadb.cli._cmd_telemetry import cmd_telemetry  # #9 Telemetría de Producto
from causadb.cli._cmd_user import cmd_user  # #10 RBAC persistente
from causadb.cli._cmd_sync import cmd_sync  # #11 Federación de ledgers
from causadb._workspace import resolve_ledger, NoWorkspaceError
from causadb.cli._cmd_workspace import cmd_workspace  # I.1 multi-workspace
from causadb.cli._cmd_explain import cmd_explain  # F.13.4.x explain
from causadb.cli._cmd_distill import cmd_distill  # F.13.4.2 distill
from causadb.cli._cmd_undo import cmd_undo  # Fase 8.4 undo
from causadb.cli._cmd_blobs import cmd_blobs  # FIX.3 blobs gc
from causadb.cli._cmd_activity import cmd_activity  # H8.5 activity


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="causadb",
        description="CausaDB causal ledger CLI (thin delegator to the nucleus).",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # config get|set|path|init|mcp [--path] [--tool|--auto] [--project] [--output]
    p_config = sub.add_parser("config", help="Get/set workspace config.")
    p_config.add_argument("action", choices=["get", "set", "path", "init", "mcp", "register-type", "delete-key"])
    p_config.add_argument("key", nargs="?", default=None, help="Config key (for 'set' action).")
    p_config.add_argument("value", nargs="?", default=None, help="Config value (for 'set' action).")
    p_config.add_argument("--fields", default=None, help="Comma-separated fields (for register-type action).")
    p_config.add_argument("--path", default=None, help="Project path (for 'init' action).")
    # MCP config generation flags (for 'mcp' action)
    mcp_grp = p_config.add_argument_group("mcp options (for 'config mcp')")
    mcp_mutex = mcp_grp.add_mutually_exclusive_group()
    mcp_mutex.add_argument(
        "--tool",
        choices=["opencode", "claude-code", "codex-cli", "cursor",
                 "windsurf", "gemini-cli", "aider"],
        default=None,
        help="Generate MCP config for a specific tool.",
    )
    mcp_mutex.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Auto-detect installed tools and generate configs.",
    )
    mcp_grp.add_argument(
        "--project", default=None,
        help="Project root path (default: CWD).",
    )
    mcp_grp.add_argument(
        "--output", default=None,
        help="Output path override (ignored for windsurf).",
    )
    p_config.set_defaults(func=cmd_config)

    # init [workspace_path] [--git-hooks] [--with-hook]
    p_init = sub.add_parser("init", help="Initialize a new CausaDB workspace.")
    p_init.add_argument("workspace", nargs="?", default=None,
                        help="Workspace directory (default: current directory).")
    p_init.add_argument("--git-hooks", action="store_true",
                        help="Install post-commit git hook for automatic COMMIT_MADE logging.")
    p_init.add_argument("--with-hook", action="store_true",
                        help="Also install shell hook for automatic command capture.")
    p_init.add_argument("--telemetry-enabled", type=lambda x: x.lower() in ("true", "1", "yes"),
                        default=None,
                        help="Enable/disable anonymous telemetry (default: prompt).")
    p_init.add_argument("--with-assistant", action="store_true",
                        help="Download and configure local AI assistant (SmolLM2 via Ollama).")
    p_init.add_argument("--no-seed-doctrina", action="store_true",
                        help="Don't write the doctrine line into the detected agent's rules file.")
    p_init.add_argument("--agent", default=None,
                         choices=["opencode", "gemini", "claude", "codex", "cursor", "windsurf", "aider", "grok", "hermes", "openjarvis", "devin"],
                        help="Force which agent's rules file gets the doctrine seed "
                             "(default: auto-detect; overrides CAUSADB_AGENT env). "
                             "Use this when multiple agents are installed (e.g. both "
                             "opencode and gemini-cli) and the heuristic picks the "
                             "wrong one.")
    p_init.add_argument("--no-genesis", action="store_true",
                        help="Skip the genesis prompt (onboarding for already-started projects). "
                             "Use in CI / non-interactive to avoid blocking.")
    p_init.set_defaults(func=cmd_init)

    # canon — imprime la guía del agente (docs/canon.md), agnóstico a tool.
    p_canon = sub.add_parser("canon", help="Print the agent guide (docs/canon.md).")
    p_canon.set_defaults(func=cmd_canon)

    # setup [--project-dir PATH] [--no-hook] [--no-git] [--no-watch] [--no-daemon] [--integrations TOOL1,TOOL2,...]
    p_setup = sub.add_parser("setup", help="One-command project setup (init + hooks + watch + daemon).")
    p_setup.add_argument("--project-dir", default=None, help="Project directory (default: CWD).")
    p_setup.add_argument("--no-hook", action="store_true", help="Skip shell hook install.")
    p_setup.add_argument("--no-git", action="store_true", help="Skip git hook install.")
    p_setup.add_argument("--no-watch", action="store_true", help="Skip watch start.")
    p_setup.add_argument("--no-daemon", action="store_true", help="Skip daemon auto-start (systemd user service).")
    p_setup.add_argument("--no-skills", action="store_true", help="Skip procedural skills registration.")
    p_setup.add_argument("--integrations", default=None, help="Comma-separated tool integrations (opencode,cursor,...).")
    p_setup.set_defaults(func=cmd_setup)

    # chronicle list|events|ref|link|rebuild-index|append|append-md|migrate|reconstruct [--bit] [--event-ids] [event_id] [--ledger]
    p_chronicle = sub.add_parser("chronicle", help="Chronicle↔Ledger cross-reference index.")
    p_chronicle.add_argument("action", choices=["list", "events", "ref", "link", "rebuild-index", "append", "append-md", "migrate", "reconstruct"])
    p_chronicle.add_argument("--bit", default=None, help="BIT name (for 'events', 'append', 'append-md' and 'reconstruct' actions).")
    p_chronicle.add_argument("--title", default=None, help="Entry title (for 'append' and 'append-md' actions).")
    p_chronicle.add_argument("--date", default=None, help="Entry date YYYY-MM-DD (for 'append' and 'append-md' actions).")
    p_chronicle.add_argument("--maker", default=None, help="Entry maker (for 'append' action).")
    p_chronicle.add_argument("--checker", default=None, help="Entry checker (for 'append' action).")
    p_chronicle.add_argument("--author", default=None, help="Entry author (for 'append-md' action).")
    p_chronicle.add_argument("--nature", default=None, help="Entry nature, e.g. 'FIX CERRADO' (for 'append-md' action).")
    p_chronicle.add_argument("--summary", default=None, help="Entry summary (for 'append' and 'append-md' actions).")
    p_chronicle.add_argument("--files", nargs="*", default=None, help="Files touched (for 'append' and 'append-md' actions).")
    p_chronicle.add_argument("--body", default=None, help="Entry body markdown (for 'append-md' action).")
    p_chronicle.add_argument("--event-id", default=None, help="Event ID to cite in **Referencias:** (for 'append-md' action).")
    p_chronicle.add_argument("--event-ids", default=None, help="Comma-separated event IDs (for 'link' action).")
    p_chronicle.add_argument("--unlinked", action="store_true", help="For 'list' action: show only BITs with no linked events (event_count == 0).")
    p_chronicle.add_argument("event_id", nargs="?", default=None, help="Event ID (for 'ref' action).")
    p_chronicle.add_argument("--ledger", default=None, help="Ledger path.")
    p_chronicle.add_argument("--chronicle-path", default=None, help="Chronicle path (for 'append-md', 'migrate' and 'rebuild-index' actions).")
    p_chronicle.add_argument("--time", default=None, help="ISO timestamp override (for 'reconstruct' action).")
    p_chronicle.set_defaults(func=cmd_chronicle)

    # log [event_json] --ledger <path> | log --decision ...
    p_log = sub.add_parser("log", help="Append a CanonicalEvent to the ledger.")
    p_log.add_argument("event_json", nargs="?", default=None, help="CanonicalEvent JSON string (omit for --decision mode).")
    p_log.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_log.add_argument("--chronicle", default=None, help="Optional chronicle path.")
    p_log.add_argument("--decision", action="store_true", help="Log a GOVERNANCE_DECISION (GAP-02).")
    p_log.add_argument("--reasoning", default=None, help="Decision reasoning (required with --decision).")
    p_log.add_argument("--impact", default=None, help="Decision impact: critical|high|medium|low (required with --decision).")
    p_log.add_argument("--decision-type", default=None, help="Decision type: strategic|architectural|tactical|revert (required with --decision).")
    p_log.add_argument("--origin", default=None, help="Decision origin: agent|distill (required with --decision).")
    p_log.add_argument("--bit", default=None, help="BIT to link the decision to (optional with --decision).")
    p_log.set_defaults(func=cmd_log)

    # replay --ledger <path>
    p_replay = sub.add_parser("replay", help="Reconstruct state from the ledger.")
    p_replay.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_replay.add_argument("--chronicle", default=None, help="Filter output to show only chronicle entries.")
    p_replay.set_defaults(func=cmd_replay)

    # activity --ledger <path> [--session] [--from-time] [--to-time]
    p_activity = sub.add_parser("activity", help="Agent Activity Report (H8.5): consolidar qué hizo un agente desde la proyección del ledger.")
    p_activity.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_activity.add_argument("--session", default=None, help="Filter by hermes_session_id (o session_id donde la proyección lo exponga).")
    p_activity.add_argument("--from-time", default=None, help="ISO 8601 inclusive lower bound.")
    p_activity.add_argument("--to-time", default=None, help="ISO 8601 inclusive upper bound.")
    p_activity.set_defaults(func=cmd_activity)

    # sentinel --ledger <path>
    p_sentinel = sub.add_parser("sentinel", help="Run sentinel drift rules.")
    p_sentinel.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_sentinel.add_argument("--chronicle", default=None, help="Optional chronicle path.")
    p_sentinel.set_defaults(func=cmd_sentinel)

    # validate --ledger <path>
    p_validate = sub.add_parser("validate", help="Validate the ledger hash chain.")
    p_validate.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_validate.add_argument("--chronicle", default=None, help="Optional chronicle path.")
    p_validate.set_defaults(func=cmd_validate)

    # query --ledger <path> [--event-type] [--ctx-id] [--parent-event-id] [--source]
    p_query = sub.add_parser("query", help="Query events via CLI index.")
    p_query.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_query.add_argument("--event-type", default=None, help="Filter by event type.")
    p_query.add_argument("--ctx-id", default=None, help="Filter by context ID.")
    p_query.add_argument("--parent-event-id", default=None, help="Filter by parent event ID.")
    p_query.add_argument("--source", default=None, help="Filter by source.")
    p_query.add_argument("--text", default=None, help="Case-insensitive substring search in event payload.")
    p_query.add_argument("--from-time", default=None, help="ISO 8601 string (inclusive lower bound).")
    p_query.add_argument("--to-time", default=None, help="ISO 8601 string (inclusive upper bound).")
    p_query.add_argument("--limit", default=None, type=int,
                         help="Máx. entradas a devolver (None usa cap por defecto = 1000).")
    p_query.set_defaults(func=cmd_query)

    # feedback --ledger <path>
    p_feedback = sub.add_parser("feedback", help="List HUMAN_FEEDBACK events from the ledger.")
    p_feedback.add_argument("--ledger", default=None, help="path (auto-discover from .causadb/ if omitted).")
    p_feedback.set_defaults(func=cmd_feedback)

    # vigilante start|stop [--ledger] [--watch] [--foreground]
    p_vigilante = sub.add_parser("vigilante", help="Start/stop the filesystem watcher (Modo Vigilante).")
    p_vigilante.add_argument("action", choices=["start", "stop"])
    p_vigilante.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_vigilante.add_argument("--watch", default=None)
    p_vigilante.add_argument("--foreground", action="store_true")
    p_vigilante.add_argument("--daemon", action="store_true",
                             help="Run in background as a daemon (fork + PID file).")
    p_vigilante.set_defaults(func=cmd_vigilante)

    # proxy --ledger <path> --model <name> --prompt <text> [--adapter openai|anthropic] --api-key <key>
    p_proxy = sub.add_parser("proxy", help="Call an LLM via proxy (auto-logs LLM_INVOKED).")
    p_proxy.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_proxy.add_argument("--model", required=True, help="Model name (e.g. gpt-4, claude-3-opus-20240229).")
    p_proxy.add_argument("--prompt", required=True, help="Prompt text to send to the LLM.")
    p_proxy.add_argument("--adapter", default="openai", choices=["openai", "anthropic", "ollama", "lmstudio"],
                         help="API adapter to use.")
    p_proxy.add_argument("--api-key", default=None, help="API key for the LLM provider (optional for ollama/lmstudio).")
    p_proxy.add_argument("--daemon", action="store_true",
                         help="Run proxy server in background as a daemon (fork + PID file).")
    p_proxy.set_defaults(func=cmd_proxy)

    # proxy-server start|stop [--ledger <path>] [--daemon]
    # P.1 — Background LLM capture proxy server
    p_proxy_server = sub.add_parser(
        "proxy-server",
        help="Start/stop the LLM capture proxy server (intercepts OpenAI/Anthropic traffic).",
    )
    p_proxy_server.add_argument("action", choices=["start", "stop"],
                                help="Action to perform.")
    p_proxy_server.add_argument("--ledger", default=None,
                                help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_proxy_server.add_argument("--daemon", action="store_true",
                                help="Run proxy server in background as a daemon (fork + PID file).")
    p_proxy_server.set_defaults(func=cmd_proxy_server)

    # sandbox --ledger <path>
    p_sandbox = sub.add_parser("sandbox", help="List sandbox mutations and violations from the ledger.")
    p_sandbox.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_sandbox.set_defaults(func=cmd_sandbox)

    # stream --ledger <path>
    p_stream = sub.add_parser("stream", help="List STREAM_INTERRUPTED events from the ledger.")
    p_stream.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_stream.set_defaults(func=cmd_stream)

    # export --format otel --ledger <path> --endpoint <url> [--headers <json>]
    p_export = sub.add_parser(
        "export",
        help="Export ledger events as OTel spans via OTLP HTTP.",
    )
    p_export.add_argument(
        "--format", default="otel", choices=["otel"],
        help="Export format (only 'otel' supported).",
    )
    p_export.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_export.add_argument(
        "--endpoint", required=True,
        help="OTLP HTTP endpoint (ej: http://localhost:6006/v1/traces).",
    )
    p_export.set_defaults(func=cmd_export)

    # import --format otel --ledger <path> --file <path>
    p_import = sub.add_parser(
        "import",
        help="Import OTLP JSON Lines spans into the CausaDB ledger.",
    )
    p_import.add_argument(
        "--format", default="otel", choices=["otel"],
        help="Import format (only 'otel' supported).",
    )
    p_import.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_import.add_argument(
        "--file", required=True,
        help="Absolute path to OTLP JSON Lines file.",
    )
    p_import.set_defaults(func=cmd_import)

    # compliance --framework <eu-ai-act|nist-ai-rmf> --ledger <path>
    p_compliance = sub.add_parser("compliance", help="Generate compliance reports.")
    p_compliance.add_argument(
        "--framework", required=True,
        choices=["eu-ai-act", "nist-ai-rmf"],
        help="Compliance framework (eu-ai-act, nist-ai-rmf).",
    )
    p_compliance.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_compliance.set_defaults(func=cmd_compliance)

    # incident --ledger <path> --event-id <uuid>
    p_incident = sub.add_parser("incident", help="Generate AI Incident Response report.")
    p_incident.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_incident.add_argument(
        "--event-id", required=True,
        help="Event ID of the incident.",
    )
    p_incident.set_defaults(func=cmd_incident)

    # audit-trail --ledger <path> --format <json|text> --output <path>
    p_audit = sub.add_parser(
        "audit-trail",
        help="Export audit trail (JSON or text).",
    )
    p_audit.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_audit.add_argument(
        "--format", default="json", choices=["json", "text"],
        help="Output format.",
    )
    p_audit.add_argument(
        "--output", default=None,
        help="Output file path (if omitted, prints to stdout).",
    )
    p_audit.set_defaults(func=cmd_audit_trail)

    # mcp-proxy start|stop|status --ledger <path> [--config <path>]
    p_mcp_proxy = sub.add_parser(
        "mcp-proxy",
        help="MCP Middleware Proxy — auto-logs TOOL_CALLED via MCP.",
    )
    p_mcp_proxy.add_argument(
        "action", choices=["start", "stop", "status"],
        help="Action to perform.",
    )
    p_mcp_proxy.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_mcp_proxy.add_argument(
        "--config", default=None,
        help="Path to proxy.json config file.",
    )
    p_mcp_proxy.add_argument(
        "--daemon", action="store_true",
        help="Run MCP proxy in background as a daemon (fork + PID file).",
    )
    p_mcp_proxy.set_defaults(func=cmd_mcp_proxy)

    # serve start|stop [--ledger <path>] [--host <ip>] [--port <int>]
    p_serve = sub.add_parser(
        "serve",
        help="Start the CausaDB REST API server (stdlib http.server, no deps).",
        description="Start the CausaDB REST API server. Default listens on 127.0.0.1:7457. "
                    "Do not expose to untrusted networks without authentication.",
    )
    p_serve.add_argument("action", choices=["start", "stop"], help="Action to perform.")
    p_serve.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=7457, help="Port (default: 7457).")
    p_serve.add_argument("--daemon", action="store_true",
                         help="Run the serve as a background daemon (double-fork + PID file).")
    p_serve.set_defaults(func=cmd_serve)

    # harvest start|stop [--ledger <path>] [--daemon]
    # BIT-CHR.41 — harvester de agentes (gemini + opencode)
    # daemonizable, invocado por `watch start` como subproceso (F.11.4).
    p_harvest = sub.add_parser(
        "harvest",
        help="Start/stop the agent-store harvester daemon (gemini-cli + opencode).",
    )
    p_harvest.add_argument("action", choices=["start", "stop"], help="Action to perform.")
    p_harvest.add_argument("--ledger", default=None,
                           help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_harvest.add_argument("--daemon", action="store_true",
                           help="Run in background as a daemon (fork + PID file).")
    p_harvest.set_defaults(func=cmd_harvest)

    # F1.1 — Génesis: onboarding one-shot para proyectos ya comenzados.
    # Para proyectos SIN daemon previo corriendo (no implementa handoff de cursor).
    p_genesis = sub.add_parser(
        "genesis",
        help="Genesis onboarding: import project history for an already-started project.",
    )
    p_genesis_sub = p_genesis.add_subparsers(dest="genesis_action")
    p_genesis_import = p_genesis_sub.add_parser(
        "import",
        help="One-shot import of project history (git/filesystem/obsidian).",
    )
    p_genesis_import.add_argument(
        "--source", default="--all",
        help="Source to import: git, filesystem, obsidian, or --all (default).",
    )
    p_genesis_import.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_genesis_import.add_argument(
        "--path", default=None,
        help="Project/source path (default: current directory).",
    )
    p_genesis_import.set_defaults(func=cmd_genesis)

    # recover <session_id> [--tool <tool>] | --search <keyword>  [--ledger <path>]
    p_recover = sub.add_parser(
        "recover",
        help="Recover full session detail from the raw agent store (Fase 13).",
    )
    p_recover.add_argument("session_id", nargs="?",
                           help="Session id to recover (tool-specific).")
    p_recover.add_argument("--search", default=None,
                           help="Keyword to search persisted storyboards instead.")
    p_recover.add_argument("--tool", default=None, choices=[
        "opencode", "gemini", "claude", "grok", "hermes", "openjarvis",
    ], help="Explicit source tool (required if the session exists in >1).")
    p_recover.add_argument("--ledger", default=None,
                           help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_recover.set_defaults(func=cmd_recover)

    # watch start|stop|status [--ledger <path>]
    p_watch = sub.add_parser("watch", help="Start/stop/status all CausaDB services (F.11.4).")
    p_watch.add_argument("action", choices=["start", "stop", "status"],
                         help="Action to perform.")
    p_watch.add_argument("--ledger", default=None,
                         help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_watch.add_argument("--daemon", action="store_true",
                         help="Start services in background as daemons.")
    p_watch.add_argument("--no-proxy", action="store_true",
                         help="Do not start the LLM capture proxy server (opt-out).")
    p_watch.add_argument("--no-serve", action="store_true",
                         help="Do not start the REST API serve daemon (headless mode).")
    p_watch.add_argument("--format", choices=["text", "json"], default=None,
                         help="Output format for 'status' action: text (human) or json (machine). Default: auto-detect (text if TTY, json if piped).")
    p_watch.set_defaults(func=cmd_watch)

    # restart [--ledger <path>] [--no-proxy] [--no-serve] [--no-systemd]
    # H-OPS.1 Fase 3 + Fase U1 — unificado: goberna el unit systemd cuando
    # está instalado (restart/start vía systemctl + forks complementarios);
    # legacy forks cuando no hay unit o con --no-systemd.
    p_restart = sub.add_parser(
        "restart",
        help="Restart CausaDB services (systemd unit if installed, legacy forks otherwise).",
    )
    p_restart.add_argument("--ledger", default=None,
                           help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_restart.add_argument("--no-proxy", action="store_true",
                           help="Skip the LLM capture proxy server.")
    p_restart.add_argument("--no-serve", action="store_true",
                           help="Skip the REST API serve daemon (ignored in systemd mode).")
    p_restart.add_argument("--no-systemd", action="store_true",
                           help="Force legacy fork mode even if the systemd unit is installed.")
    p_restart.add_argument("--dry-run", action="store_true",
                           help="Simulate restart without executing anything. Outputs JSON with would_execute actions.")
    p_restart.add_argument("--format", choices=["text", "json"], default="json",
                           help="Output format for dry-run: text or json (default: json).")
    p_restart.set_defaults(func=cmd_restart)

    # opencode-config [--project <path>] [--output <path>]
    p_oc_config = sub.add_parser(
        "opencode-config",
        help="Generate opencode MCP config (opencode.json) for CausaDB (F.11.5).",
    )
    p_oc_config.add_argument(
        "--project", default=None,
        help="Project root path (default: CWD).",
    )
    p_oc_config.add_argument(
        "--output", default=None,
        help="Output path (default: <project>/opencode.json, merges existing config).",
    )
    p_oc_config.set_defaults(func=cmd_opencode_config)

    # audit [--format markdown|json|terminal] [--output file] [--repo path]
    # Standalone (F.12.6) — no --ledger, no .causadb/ workspace required.
    p_audit = sub.add_parser(
        "audit",
        help="Measure AI-authored code survival in git history (standalone).",
    )
    p_audit.add_argument(
        "--format", default="terminal",
        choices=["markdown", "json", "terminal"],
        help="Output format (default: terminal).",
    )
    p_audit.add_argument(
        "--output", default=None,
        help="Output file path (if omitted, prints to stdout).",
    )
    p_audit.add_argument(
        "--repo", default=None,
        help="Path to the git repository (default: CWD).",
    )
    p_audit.set_defaults(func=cmd_audit)

    # ocb status|close|purge --ledger <path>
    p_ocb = sub.add_parser(
        "ocb",
        help="Operational Context Buffer — L1 short-term memory management.",
    )
    p_ocb.add_argument(
        "action", choices=["status", "close", "purge", "rebuild"],
        help="Action to perform.",
    )
    p_ocb.add_argument(
        "--ledger", default=None,
        help="path (auto-discover from .causadb/ if omitted).",
    )
    p_ocb.add_argument(
        "--summary", default=None,
        help="Summary JSON for close action.",
    )
    p_ocb.add_argument(
        "--keep-last", type=int, default=None,
        help="Keep N latest partitions (purge action).",
    )
    p_ocb.add_argument(
        "--older-than-days", type=int, default=None,
        help="Delete partitions older than N days (purge action).",
    )
    p_ocb.set_defaults(func=cmd_ocb)

    # F.12.4 impact — downstream causal cone of an event.
    p_impact = sub.add_parser(
        "impact",
        help="Show the downstream causal cone of an event (blast radius).",
    )
    p_impact.add_argument(
        "event_id",
        help="Event ID to trace downstream from.",
    )
    p_impact.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_impact.set_defaults(func=cmd_impact)

    # F.12.5 bisect — binary search for the first action that broke the test.
    p_bisect = sub.add_parser(
        "bisect",
        help="Binary search the event chain for the first action that broke a test command.",
    )
    p_bisect.add_argument(
        "--test", required=True,
        help="Shell command to run against the restored workspace (exit 0 = pass).",
    )
    p_bisect.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_bisect.set_defaults(func=cmd_bisect)

    # F.12.2 why — attribute a line to the event that introduced it.
    p_why = sub.add_parser(
        "why",
        help="Attribute a line to the event that introduced it (causal blame).",
    )
    p_why.add_argument(
        "target",
        help="'<file>:<line>' — e.g. 'main.py:42'.",
    )
    p_why.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_why.set_defaults(func=cmd_why)

    # F.12.3 trace — upstream causal cone of a line.
    p_trace = sub.add_parser(
        "trace",
        help="Show the upstream causal cone of a line (transitive causes).",
    )
    p_trace.add_argument(
        "target",
        help="'<file>:<line>' — e.g. 'main.py:42'.",
    )
    p_trace.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_trace.set_defaults(func=cmd_trace)

    # R.1 resume — generate session resume summary from OCB + ledger replay.
    p_resume = sub.add_parser(
        "resume",
        help="Generate a session resume summary (merges OCB context + ledger replay).",
    )
    p_resume.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_resume.add_argument(
        "--format", default="json", choices=["json", "markdown"],
        help="Output format (default: json).",
    )
    p_resume.add_argument(
        "--write", action="store_true",
        help="Write RESUME.md next to the OCB directory.",
    )
    p_resume.set_defaults(func=cmd_resume)

    # F.13.3.4 score — unified churn/waste/survival score.
    p_score = sub.add_parser(
        "score",
        help="Compute the unified churn/waste/survival score (F.13.3).",
    )
    p_score.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_score.add_argument(
        "--format", default="json",
        choices=["json", "md", "markdown", "terminal"],
        help="Output format (default: json). 'md' is an alias for 'markdown'.",
    )
    p_score.add_argument(
        "--session", default=None,
        help="Score only this session (ctx_id).",
    )
    p_score.add_argument(
        "--by-session", action="store_true",
        help="Include per-session breakdown.",
    )
    p_score.set_defaults(func=cmd_score)

    # F.13.4.x explain — explicar decisiones de gobernanza
    p_explain = sub.add_parser(
        "explain",
        help="Explain a governance decision by tracing its causal lineage."
    )
    p_explain.add_argument(
        "event_id",
        help="Event ID of the GOVERNANCE_DECISION to explain."
    )
    p_explain.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted)."
    )
    p_explain.set_defaults(func=cmd_explain)

    # F.13.4.2 distill — exponer motor de distill como comando CLI
    p_distill = sub.add_parser(
        "distill",
        help="Run the distill engine to extract skills from the ledger."
    )
    p_distill.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted)."
    )
    p_distill.add_argument(
        "--format", default="json",
        choices=["json", "md", "markdown", "terminal"],
        help="Output format (default: json). 'md' is an alias for 'markdown'."
    )
    p_distill.set_defaults(func=cmd_distill)

    # Fase 8.4 — undo: restaurar archivo desde último snapshot en BlobStore
    p_undo = sub.add_parser("undo", help="Restore a file from the last known good snapshot.")
    p_undo.add_argument("--file", required=True, help="Path to the file to restore.")
    p_undo.add_argument("--ledger", default=None, help="Ledger path.")
    p_undo.set_defaults(func=cmd_undo)

    # FIX.3 — blobs gc: BlobStore garbage collection (dry-run by default).
    p_blobs = sub.add_parser("blobs", help="BlobStore garbage collection and inspection.")
    p_blobs.set_defaults(func=cmd_blobs)
    p_blobs_sub = p_blobs.add_subparsers(dest="blobs_action", metavar="<blobs-action>")
    p_blobs_gc = p_blobs_sub.add_parser("gc", help="Collect/report orphan blobs (dry-run by default).")
    p_blobs_gc.add_argument("--ledger", default=None, help="Ledger path (auto-discover from .causadb/ if omitted).")
    p_blobs_gc.add_argument("--execute", action="store_true", help="Move orphans to .trash/ (requires valid ledger).")
    p_blobs_gc.set_defaults(func=cmd_blobs)

    # shell-hook install|remove|status|flush [--ledger] [--ctx-id]
    p_shell = sub.add_parser(
        "shell-hook",
        help="Install/remove/status/flush shell command hook (bash-only).",
    )
    p_shell.add_argument(
        "action", choices=["install", "remove", "status", "flush"],
        help="Action to perform.",
    )
    p_shell.add_argument(
        "--ledger", default=None,
        help="Ledger path (for flush action).",
    )
    p_shell.add_argument(
        "--ctx-id", default="shell",
        help="Context ID for shell events (default: shell).",
    )
    p_shell.set_defaults(func=cmd_shell_hook)

    # R.2 revive — generate volatile revival context
    p_revive = sub.add_parser(
        "revive",
        help="Generate volatile revival context for agent bootstrap (R.2).",
    )
    p_revive.add_argument(
        "--ledger", default=None,
        help="Ledger path (auto-discover from .causadb/ if omitted).",
    )
    p_revive.add_argument(
        "--last", action="store_true",
        help="Use the last recorded workspace (~/.causadb/last_workspace.json).",
    )
    p_revive.add_argument(
        "--format", default="markdown", choices=["json", "markdown"],
        help="Output format (default: markdown).",
    )
    p_revive.add_argument(
        "--decisions", type=int, default=10,
        help="Maximum number of governance decisions to include (default: 10).",
    )
    p_revive.add_argument(
        "--write", default=None,
        help="Write output to file path (optional).",
    )
    p_revive.set_defaults(func=cmd_revive)

    # R.3.4 snapshot
    p_snapshot = sub.add_parser("snapshot", help="Snapshot project state (R.3.4).")
    p_snapshot.add_argument("--ledger", default=None, help="Ledger path.")
    p_snapshot.add_argument("--tests", type=int, required=True, help="Total tests passed.")
    p_snapshot.add_argument("--fases", default="", help="Fases completadas (comma-separated).")
    p_snapshot.add_argument("--bloqueantes", type=int, default=0, help="Bloqueantes resueltos.")
    p_snapshot.add_argument("--notas", default="", help="Notas adicionales.")
    p_snapshot.add_argument("--chronicle-ref", default=None, help="Associate snapshot with a BIT entry.")
    p_snapshot.set_defaults(func=cmd_snapshot)

    # D.1 daemon install|start|stop|status [--ledger <path>]
    p_daemon = sub.add_parser(
        "daemon",
        help="Manage CausaDB as a systemd user service (D.1).",
    )
    p_daemon.add_argument(
        "action", choices=["install", "start", "stop", "status"],
        help="Action to perform.",
    )
    p_daemon.add_argument(
        "--ledger", default=None,
        help="Ledger path (required for install action).",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    # I.1 workspace create|list|delete|switch|current [--root-dir]
    p_workspace = sub.add_parser(
        "workspace",
        help="Manage multi-workspace environments (I.1).",
    )
    p_workspace.add_argument(
        "action",
        choices=["create", "list", "delete", "switch", "current"],
        help="Action to perform.",
    )
    p_workspace.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Workspace name (required for create, delete, switch).",
    )
    p_workspace.add_argument(
        "--root-dir",
        default=None,
        help="Workspace root directory (default: ~/.causadb/workspaces/).",
    )
    p_workspace.set_defaults(func=cmd_workspace)

    # Crash reporter — list, delete, export
    p_crash = sub.add_parser("crash", help="Manage crash reports (list, delete, export).")
    p_crash.add_argument("action", choices=["list", "delete", "export"],
                         help="Action to perform.")
    p_crash.add_argument("--crash-id", default=None,
                         help="Specific crash ID to delete (for 'delete' action).")
    p_crash.set_defaults(func=cmd_crash)

    # Telemetry (#6 Privacidad Opt-out, #9 Telemetría de Producto)
    p_telemetry = sub.add_parser(
        "telemetry",
        help="Manage anonymous usage telemetry (status, on, off, export).",
    )
    p_telemetry.add_argument(
        "telemetry_action",
        choices=["status", "on", "off", "export"],
        help="Action to perform.",
    )
    p_telemetry.set_defaults(func=cmd_telemetry)

    # Ola 2A — auto-update
    p_update = sub.add_parser("update", help="Check for and install CausaDB updates from GitHub Releases.")
    p_update.add_argument("--check", action="store_true",
                          help="Only check for updates, do not download or install.")
    p_update.set_defaults(func=cmd_update)

    # #10 RBAC persistente — user management
    p_user = sub.add_parser("user", help="Manage RBAC users (add, remove, list).")
    p_user_sub = p_user.add_subparsers(dest="user_action", metavar="<action>")

    p_user_add = p_user_sub.add_parser("add", help="Add a new user.")
    p_user_add.add_argument("--username", required=True, help="Username.")
    p_user_add.add_argument("--password", required=True, help="Password.")
    p_user_add.add_argument("--role", default="member",
                            choices=["admin", "member", "auditor"],
                            help="Role (default: member).")

    p_user_remove = p_user_sub.add_parser("remove", help="Remove a user.")
    p_user_remove.add_argument("--username", required=True, help="Username.")

    p_user_list = p_user_sub.add_parser("list", help="List all users.")

    p_user.set_defaults(func=cmd_user)

    # #11 Federación de ledgers — hub-and-spoke sync
    p_sync = sub.add_parser("sync", help="Ledger federation (hub-and-spoke sync).")
    p_sync.add_argument(
        "sync_action",
        choices=["status", "push", "pull", "full", "config"],
        help="Sync action to perform.",
    )
    p_sync.add_argument(
        "--hub-url", default=None,
        help="Hub server URL (for config action).",
    )
    p_sync.add_argument(
        "--api-key", default=None,
        help="API key for hub auth (for config action).",
    )
    p_sync.add_argument(
        "--interval", type=int, default=60,
        help="Sync interval in minutes (for config action, default: 60).",
    )
    p_sync.add_argument(
        "--ledger", default=None,
        help="Ledger path (optional, auto-discovered from workspace).",
    )
    p_sync.set_defaults(func=cmd_sync)

    return parser


def main(args=None) -> int:
    """CLI entrypoint. Returns the exit code (does not call sys.exit directly)."""
    # Install global excepthook to auto-capture unhandled exceptions
    try:
        from causadb._crash_reporter import save_global_excepthook
        save_global_excepthook()
    except Exception:
        pass

    parser = build_parser()
    parsed = parser.parse_args(args)

    # No subcommand → show help, exit 0 (argparse default behaviour for our case).
    if not getattr(parsed, "command", None):
        print("CausaDB — registro causal para agentes de IA")
        print()
        print("Para empezar:")
        print("  causadb init       crear un nuevo proyecto")
        print("  causadb setup      configurar todo automáticamente")
        print("  causadb validate   verificar integridad del ledger")
        print()
        parser.print_help()
        return 0

    # Ola 2A — startup update hook (skip for 'update' command itself and quiet mode).
    if parsed.command != "update":
        _hook_check_update(parsed)

    # Resolve --ledger removed from main.py — each handler calls
    # resolve_ledger() explicitly when needed (Artículo II).
    # Handlers that need a ledger path validate it themselves.
    # This prevents the silent None propagation bug.

    # Telemetry counter hooks (#9) — anonymous usage counters.
    _instrument_telemetry(parsed)

    # Delegate to the subcommand handler — returns (exit_code, output_str).
    try:
        exit_code, output = parsed.func(parsed)
        if exit_code != 0:
            try:
                import json as _json
                data = _json.loads(output)
                data.pop("error_type", None)
                output = _json.dumps(data)
            except Exception:
                pass
        print(output)
        return exit_code
    except Exception:
        from causadb._crash_reporter import save_crash
        save_crash()
        raise


def _hook_check_update(parsed) -> None:
    """Check for updates on CLI startup. Skip if quiet or non-interactive."""
    import os
    import json as _json
    import http.client as _http_client

    # Skip if --quiet flag or stdin is not a tty
    if getattr(parsed, "quiet", False):
        return
    if not sys.stdin.isatty():
        return

    # Check skip_version in config
    skip_version = _load_skip_version()
    current = None
    latest = None

    # Try daemon first
    try:
        conn = _http_client.HTTPConnection("127.0.0.1", 7457, timeout=3)
        conn.request("GET", "/api/check-update")
        resp = conn.getresponse()
        if resp.status == 200:
            data = _json.loads(resp.read())
            current_version = data.get("current_version", "")
            latest = data.get("latest_version", "")
        conn.close()
    except Exception:
        pass

    # Fallback: direct GitHub check
    if latest is None:
        try:
            from causadb._updater import check_update
            result = check_update()
            current_version = result.get("current_version", "")
            latest = result.get("latest_version", "")
        except Exception:
            return

    if not latest or not current_version:
        return

    # Skip if already up to date
    if not _version_gt(latest.lstrip("v"), current_version.lstrip("v")):
        return

    # Skip if skip_version matches
    if skip_version and skip_version == latest:
        return

    # Show banner
    print(f"\n⚡ CausaDB {latest} available (you have {current_version}).")
    try:
        answer = input("Update now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer in ("y", "yes"):
        import subprocess as _sp
        _sp.run([sys.executable, "-m", "causadb.cli.main", "update"])
    else:
        _save_skip_version(latest)


def _load_skip_version() -> str:
    """Load skip_version from .causadb/config.json if it exists."""
    import os as _os
    from causadb._workspace import WorkspaceManager
    config_path = WorkspaceManager.discover(_os.getcwd())
    if config_path is None:
        return ""
    try:
        with open(config_path) as f:
            data = _json.loads(f.read())
        return data.get("skip_version", "")
    except Exception:
        return ""


def _save_skip_version(version: str) -> None:
    """Save skip_version to .causadb/config.json."""
    import os as _os
    from causadb._workspace import WorkspaceManager
    config_path = WorkspaceManager.discover(_os.getcwd())
    if config_path is None:
        return
    try:
        with open(config_path) as f:
            data = _json.loads(f.read())
        data["skip_version"] = version
        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            _os.fsync(f.fileno())
        _os.replace(tmp, config_path)
    except Exception:
        pass


def _instrument_telemetry(parsed) -> None:
    """Increment anonymous usage counters for CLI invocations (#9).

    Respects ``telemetry.enabled`` opt-out — no-op when disabled.
    """
    try:
        from causadb._telemetry import increment
        cmd = getattr(parsed, "command", None)
        if cmd:
            # Map public command names to counter keys
            counter_map = {
                "score": "causadb_score_called",
                "replay": "causadb_replay_called",
                "query": "causadb_query_called",
                "validate": "causadb_validate_called",
                "audit": "causadb_audit_called",
                "trace": "causadb_trace_called",
                "why": "causadb_why_called",
                "impact": "causadb_impact_called",
                "bisect": "causadb_bisect_called",
                "init": "causadb_init_called",
                "telemetry": "causadb_telemetry_called",
                "serve": "causadb_dashboard_started",
            }
            counter = counter_map.get(cmd)
            if counter:
                increment(counter)
    except Exception:
        pass  # Telemetry failure must never break the CLI


if __name__ == "__main__":
    sys.exit(main())
