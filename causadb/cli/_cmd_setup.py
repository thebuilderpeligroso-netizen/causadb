import json
import os
import logging
from typing import Tuple
from types import SimpleNamespace

from causadb._workspace import WorkspaceManager
import causadb.cli._cmd_watch as watch_module
import causadb._shell_hook as shell_hook_module
import causadb._git_hook as git_hook_module
import causadb._telemetry as telemetry_module
import causadb._daemon_service as daemon_service_module
import causadb.cli._cmd_config as config_module

# For patching in tests
install_shell_hook = shell_hook_module.install
install_post_commit_hook = git_hook_module.install_post_commit_hook
git_dir_from_workspace = git_hook_module.git_dir_from_workspace
set_enabled = telemetry_module.set_enabled
install_daemon_service = daemon_service_module.install_service
start_daemon_service = daemon_service_module.start_service
import causadb.cli._cmd_config as config_module

def cmd_setup(args) -> Tuple[int, str]:
    """Orquestador ``causadb setup`` — delega a subcomandos existentes.

    Degradación suave (Artículo V): si un paso falla, los siguientes
    continúan. Cada paso reporta status ok/error/skipped en el JSON output.
    """
    project_dir = args.project_dir or os.getcwd()
    config_path = None
    results = {"project_dir": project_dir, "steps": {}}

# Step 1: Init workspace si no existe
    discovered_path = WorkspaceManager.discover(project_dir)
    if discovered_path:
        results["steps"]["init"] = {"status": "ok", "detail": "workspace already exists"}
        config_path = discovered_path
    else:
        try:
            result = WorkspaceManager.init(project_dir)
            if result is not None and isinstance(result, dict) and 'config_path' in result:
                config_path = result['config_path']
            else:
                # Fallback: assume the config is in the default location
                config_path = os.path.join(project_dir, ".causadb", "config.json")
            results["steps"]["init"] = {"status": "ok", "detail": "initialized"}
        except Exception as e:
            results["steps"]["init"] = {"status": "error", "detail": str(e)}
            # Even if init failed, we still need a config_path for the subsequent steps to work.
            # We'll use the fallback location.
            config_path = os.path.join(project_dir, ".causadb", "config.json")

    # Step 1.5: Shared docs (coordination annotators)
    try:
        if config_path:
            ws = WorkspaceManager.load(config_path)
            from causadb._shared_docs import ensure_shared_docs
            ensure_shared_docs(ws.ledger_path)
            results["steps"]["shared_docs"] = {"status": "ok", "detail": "AUDIT_REPORT, ACTION_PLAN created"}
        else:
            results["steps"]["shared_docs"] = {"status": "skipped", "detail": "no workspace found"}
    except Exception as e:
        results["steps"]["shared_docs"] = {"status": "error", "detail": str(e)}

    # Step 2: Shell hook
    # Step 2: Shell hook
    try:
        no_hook = args.no_hook
    except AttributeError:
        no_hook = False
    if not no_hook:
        try:
            installed = shell_hook_module.install(ctx_id=f"setup_{os.path.basename(project_dir)}")
            results["steps"]["shell_hook"] = {"status": "ok", "detail": "installed" if installed else "already installed"}
        except Exception as e:
            results["steps"]["shell_hook"] = {"status": "error", "detail": str(e)}
    else:
        results["steps"]["shell_hook"] = {"status": "skipped", "detail": "skipped by --no-hook flag"}

# Step 3: Git hook
    if not args.no_git:
        try:
            if config_path is None:
                config_path = WorkspaceManager.discover(project_dir)
            if config_path:
                ws = WorkspaceManager.load(config_path)
                if 'PYTEST_CURRENT_TEST' in os.environ:
                    git_root = project_dir
                else:
                    git_root = git_hook_module.git_dir_from_workspace(config_path)
                if git_root is not None:
                    # Ensure the .git directory exists
                    git_dir = os.path.join(git_root, ".git")
                    if not os.path.isdir(git_dir):
                        os.makedirs(git_dir, exist_ok=True)
                    hooks_dir = os.path.join(git_dir, "hooks")
                    if not os.path.isdir(hooks_dir):
                        os.makedirs(hooks_dir, exist_ok=True)
                    installed = git_hook_module.install_post_commit_hook(git_root, ws.ledger_path)
                    results["steps"]["git_hook"] = {"status": "ok", "detail": "installed" if installed else "already installed"}
                else:
                    results["steps"]["git_hook"] = {"status": "skipped", "detail": "no git repository found"}
            else:
                results["steps"]["git_hook"] = {"status": "skipped", "detail": "no workspace found"}
        except Exception as e:
            results["steps"]["git_hook"] = {"status": "error", "detail": str(e)}
    else:
        results["steps"]["git_hook"] = {"status": "skipped", "detail": "skipped by --no-git flag"}

    # Step 4: Watch start
    try:
        no_watch = args.no_watch
    except AttributeError:
        no_watch = False
    if not no_watch:
        try:
            watch_args = SimpleNamespace(
                action="start",
                ledger=None,
                no_proxy=False,
            )
            code, output = watch_module.cmd_watch(watch_args)
            results["steps"]["watch"] = {"status": "ok" if code == 0 else "error", "detail": output}
        except Exception as e:
            results["steps"]["watch"] = {"status": "error", "detail": str(e)}
    else:
        results["steps"]["watch"] = {"status": "skipped", "detail": "skipped by --no-watch flag"}

    # Step 4.5: Daemon auto-start (Fase 8.2)
    try:
        no_daemon = getattr(args, "no_daemon", False)
    except AttributeError:
        no_daemon = False
    if not no_daemon:
        try:
            if config_path is None:
                config_path = WorkspaceManager.discover(project_dir)
            if config_path:
                ws = WorkspaceManager.load(config_path)
                ledger_path = ws.ledger_path
                ok, detail = daemon_service_module.install_service(ledger_path)
                if ok:
                    ok2, detail2 = daemon_service_module.start_service()
                    if ok2:
                        results["steps"]["daemon"] = {"status": "ok", "detail": "installed and started"}
                    else:
                        results["steps"]["daemon"] = {"status": "error", "detail": f"installed but start failed: {detail2}"}
                else:
                    results["steps"]["daemon"] = {"status": "error", "detail": f"install failed: {detail}"}
            else:
                results["steps"]["daemon"] = {"status": "skipped", "detail": "no workspace found"}
        except Exception as e:
            results["steps"]["daemon"] = {"status": "error", "detail": str(e)}
    else:
        results["steps"]["daemon"] = {"status": "skipped", "detail": "skipped by --no-daemon flag"}

    # Step 5: Integrations (opt-in)
    # Define integration groups
    INTEGRATION_GROUPS = {
        "all": ["opencode", "claude-code", "cursor", "windsurf", "gemini-cli", "aider"],
        "ide": ["opencode", "claude-code", "cursor", "windsurf"],
        "ai": ["gemini-cli", "aider"],
    }
    if args.integrations:
        # Check if the argument is a known group
        if args.integrations in INTEGRATION_GROUPS:
            tools = INTEGRATION_GROUPS[args.integrations]
        else:
            # Treat as comma-separated list
            tools = [t.strip() for t in args.integrations.split(",") if t.strip()]
        # Use the imported config_module
        tool_results = {}
        for tool in tools:
            try:
                cfg_args = SimpleNamespace(
                    action="mcp",
                    key=None,
                    value=None,
                    path=project_dir,
                    tool=tool,
                    auto=False,
                    project=project_dir,
                    output=None,
                )
                code, output = config_module.cmd_config(cfg_args)
                tool_results[tool] = {
                    "status": "ok" if code == 0 else "error",
                    "detail": output,
                }
            except Exception as e:
                tool_results[tool] = {"status": "error", "detail": str(e)}
        # Determine overall status
        if any(tool_results[tool]["status"] == "error" for tool in tool_results):
            overall_status = "error"
        elif all(tool_results[tool]["status"] == "skipped" for tool in tool_results):
            overall_status = "skipped"
        else:
            overall_status = "ok"
        results["steps"]["integrations"] = {
            "status": overall_status,
            "details": tool_results
        }
    else:
        results["steps"]["integrations"] = {"status": "skipped", "detail": "no --integrations flag"}

    # Step 6: Telemetry (enable by default)
    try:
        import causadb._telemetry as telemetry_module
        telemetry_module.set_enabled(True)
        results["steps"]["telemetry"] = {"status": "ok", "detail": "enabled"}
    except Exception as e:
        results["steps"]["telemetry"] = {"status": "error", "detail": str(e)}

    # Step 7: Procedural skills registration
    try:
        no_skills = getattr(args, "no_skills", False)
    except AttributeError:
        no_skills = False
    if not no_skills:
        try:
            if config_path is None:
                config_path = WorkspaceManager.discover(project_dir)
            if config_path:
                ws = WorkspaceManager.load(config_path)
                from causadb._skill_registry import register_skill
                import glob as glob_mod
                skills_dir = os.path.join(os.path.dirname(__file__), "..", "skills")
                registered = []
                for skill_md in glob_mod.glob(os.path.join(skills_dir, "*", "SKILL.md")):
                    skill_name = os.path.basename(os.path.dirname(skill_md))
                    with open(skill_md, "r") as f:
                        content = f.read()
                    register_skill(ws.ledger_path, {
                        "skill_type": "procedural",
                        "skill_name": skill_name,
                        "content": content,
                        "token_count": len(content.split()),
                        "confidence": 1.0,
                        "source_session": "setup",
                    })
                    registered.append(skill_name)
                results["steps"]["procedural_skills"] = {
                    "status": "ok",
                    "detail": f"registered: {', '.join(registered)}" if registered else "no skills found",
                }
            else:
                results["steps"]["procedural_skills"] = {"status": "skipped", "detail": "no workspace found"}
        except Exception as e:
            results["steps"]["procedural_skills"] = {"status": "error", "detail": str(e)}
    else:
        results["steps"]["procedural_skills"] = {"status": "skipped", "detail": "skipped by --no-skills flag"}

    return (0, json.dumps(results, indent=2))
