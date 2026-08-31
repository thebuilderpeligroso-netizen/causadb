"""P.2 — CLI subcommand for causadb proxy

This module provides TWO functions:

1. ``cmd_proxy`` — One-shot call-style proxy (--model, --prompt, --adapter).
   Uses LLMProxy.call_llm() and logs LLM_INVOKED to the ledger.

2. ``cmd_proxy_server`` — Background capture proxy server (start|stop).
   Uses LLMProxyServer to intercept all LLM traffic (prompts, reasoning,
   tokens, cost) and log both LLM_INVOKED + REASONING_STEP to the ledger.

Thin wrappers (Article II — no logic duplicated).
"""

import json
import os
import signal
import sys
from typing import Tuple

from causadb._daemon import get_daemon
from causadb._proxy import LLMProxy
from causadb._llm_proxy_server import LLMProxyServer
from causadb._secrets import Secrets
from causadb._workspace import resolve_ledger, NoWorkspaceError


# ---------------------------------------------------------------------------
# 1. Call-style proxy (one-shot LLM call)
# ---------------------------------------------------------------------------

def cmd_proxy(args) -> Tuple[int, str]:
    """Call an LLM via the proxy adapter and log LLM_INVOKED to the ledger.

    This is the original F.3.3 one-shot proxy: it calls ``call_llm`` once
    and returns the response text.
    """
    try:
        ledger_path = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))

    api_key = args.api_key
    if not api_key:
        try:
            api_key = Secrets.get("openai_key")
        except KeyError:
            pass
    if not api_key:
        try:
            api_key = Secrets.get("anthropic_key")
        except KeyError:
            pass
    proxy = LLMProxy(ledger_path=ledger_path, api_key=api_key)

    try:
        content = proxy.call_llm(
            model=args.model,
            prompt=args.prompt,
            adapter=args.adapter,
        )
        return (0, json.dumps({"content": content, "model": args.model, "adapter": args.adapter}))
    except ValueError as e:
        return (1, json.dumps({"error": str(e)}))
    except Exception as e:
        return (1, json.dumps({"error": str(e)}))


# ---------------------------------------------------------------------------
# 2. Proxy server (background capture proxy — start|stop)
# ---------------------------------------------------------------------------

def cmd_proxy_server(args) -> Tuple[int, str]:
    """Route ``causadb proxy-server start|stop``."""
    try:
        ledger_path = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))

    action = args.action
    daemon = getattr(args, "daemon", False)

    if action == "start":
        return _start_server(ledger_path, daemon)
    elif action == "stop":
        return _stop_server(ledger_path)
    else:
        return (1, json.dumps({"error": f"Unknown action: {action}"}))


def _start_server(ledger_path: str, daemon: bool = False) -> Tuple[int, str]:
    """Start the LLM capture proxy server."""
    platform_daemon = get_daemon()
    if daemon:
        if platform_daemon.is_running("proxy_server"):
            return (0, json.dumps({"status": "already_running", "ledger": ledger_path}))
        platform_daemon.daemonize("proxy_server")

    ledger_dir = os.path.dirname(ledger_path)
    capture_path = os.path.join(ledger_dir, "capture", "proxy_exchanges.jsonl")

    server = LLMProxyServer(
        ledger_path=ledger_path,
        host="127.0.0.1",
        port=4242,
        capture_path=capture_path,
    )

    output = json.dumps({
        "status": "started",
        "ledger": ledger_path,
        "proxy": "http://127.0.0.1:4242",
        "openai_base_url": "http://127.0.0.1:4242/openai/v1",
        "anthropic_base_url": "http://127.0.0.1:4242/anthropic/v1",
        "capture": capture_path,
    })

    if daemon:
        print(output, flush=True)

        def _handle_sigterm(signum, frame):
            server.stop()

        signal.signal(signal.SIGTERM, _handle_sigterm)
        server.start()
        return (0, json.dumps({"status": "terminated", "ledger": ledger_path}))

    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    return (0, output)


def _stop_server(ledger_path: str) -> Tuple[int, str]:
    """Stop the proxy server daemon."""
    platform_daemon = get_daemon()
    if platform_daemon.is_running("proxy_server"):
        platform_daemon.kill("proxy_server")
        return (0, json.dumps({"status": "stopped", "ledger": ledger_path}))
    return (0, json.dumps({"status": "not_running", "ledger": ledger_path}))
