"""P.1 — LLM Capture Proxy Server (stdlib only).

HTTP server that intercepts OpenAI/Anthropic API calls, captures prompts,
responses, reasoning, tokens, and cost, and logs everything to the CausaDB
ledger (LLM_INVOKED + REASONING_STEP events).

Usage::

    server = LLMProxyServer(
        ledger_path="/path/to/ledger.log",
        host="127.0.0.1",
        port=4242,
        openai_upstream="https://api.openai.com",
        anthropic_upstream="https://api.anthropic.com",
        capture_path="/tmp/capture.jsonl",
    )
    server.start()  # blocks until stop_event is set

Design (Article VIII — concrete, no abstract base):
  - One handler class per protocol variant.
  - Routing by URL path prefix.
  - Degradación suave: never crash on parse errors.
"""

import http.server
import json
import os
import threading
import time
import urllib.request
from types import MappingProxyType
from typing import Optional

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENAI_PATH = "/openai/v1"
_ANTHROPIC_PATH = "/anthropic/v1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_openai_prompt(body: dict) -> str:
    """Concatenate all user messages from an OpenAI-style request body."""
    messages = body.get("messages", [])
    parts = []
    for m in messages:
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c["text"] for c in content if c.get("type") == "text"
                )
            parts.append(str(content))
    return "\n".join(parts)


def _extract_openai_response(body: dict) -> tuple:
    """Extract (response_text, reasoning, tokens_in, tokens_out) from OpenAI response."""
    response_text = ""
    reasoning = ""
    tokens_in = 0
    tokens_out = 0

    try:
        usage = body.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
    except Exception:
        pass

    try:
        choices = body.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            response_text = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content", "") or ""
        if not reasoning:
            # Fallback: check delta field (non-standard)
            try:
                choice = body.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                reasoning = delta.get("reasoning_content", "") or ""
            except Exception:
                pass
    except Exception:
        pass

    return response_text, reasoning, tokens_in, tokens_out


def _extract_anthropic_prompt(body: dict) -> str:
    """Concatenate user messages from Anthropic-style request body."""
    messages = body.get("messages", [])
    parts = []
    for m in messages:
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c["text"] for c in content if c.get("type") == "text"
                )
            parts.append(str(content))
    return "\n".join(parts)


def _extract_anthropic_response(body: dict) -> tuple:
    """Extract (response_text, reasoning, tokens_in, tokens_out) from Anthropic response."""
    response_text = ""
    reasoning = ""
    tokens_in = 0
    tokens_out = 0

    try:
        usage = body.get("usage", {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
    except Exception:
        pass

    try:
        content_blocks = body.get("content", [])
        reasoning_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                response_text = block.get("text", "") or ""
            elif block.get("type") == "thinking":
                reasoning_parts.append(block.get("thinking", "") or "")
        reasoning = "\n".join(reasoning_parts)
    except Exception:
        pass

    return response_text, reasoning, tokens_in, tokens_out


def _calculate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Approximate cost in USD based on model prefix.

    Returns 0.0 for unknown models (degradación suave).
    """
    model_lower = model.lower()

    # Claude 4 / Opus 4
    if "claude-4" in model_lower or "opus-4" in model_lower:
        return tokens_in * 15.0 / 1_000_000 + tokens_out * 75.0 / 1_000_000

    # Opus 3.5 / 3
    if "claude-3-opus" in model_lower or "claude-3.5" in model_lower:
        return tokens_in * 15.0 / 1_000_000 + tokens_out * 75.0 / 1_000_000

    # Sonnet 4 / 3.5
    if ("claude-sonnet-4" in model_lower
            or "claude-3.5-sonnet" in model_lower
            or "claude-3-5-sonnet" in model_lower):
        return tokens_in * 3.0 / 1_000_000 + tokens_out * 15.0 / 1_000_000

    # Sonnet 3
    if "claude-3-sonnet" in model_lower:
        return tokens_in * 3.0 / 1_000_000 + tokens_out * 15.0 / 1_000_000

    # Haiku 3.5
    if "claude-3.5-haiku" in model_lower or "claude-3-haiku" in model_lower:
        return tokens_in * 0.8 / 1_000_000 + tokens_out * 4.0 / 1_000_000

    # GPT-4o
    if "gpt-4o" in model_lower:
        return tokens_in * 2.5 / 1_000_000 + tokens_out * 10.0 / 1_000_000

    # GPT-4
    if "gpt-4" in model_lower:
        return tokens_in * 30.0 / 1_000_000 + tokens_out * 60.0 / 1_000_000

    # GPT-3.5 / o1 / o3
    if "gpt-3.5" in model_lower or "o1" in model_lower or "o3" in model_lower:
        return tokens_in * 1.5 / 1_000_000 + tokens_out * 2.0 / 1_000_000

    return 0.0


# ---------------------------------------------------------------------------
# Capture file logger
# ---------------------------------------------------------------------------

class CaptureLogger:
    """Append structured capture entries to a JSONL file.

    Directory creation is deferred to first write, and write errors are
    swallowed (degradación suave — the proxy must keep serving even if
    the capture file is unwritable).
    """

    def __init__(self, path: str):
        self.path = path
        self._dir_ensured = False

    def log(self, entry: dict):
        try:
            if not self._dir_ensured:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self._dir_ensured = True
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with open(self.path, "a") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class _ProxyHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Single HTTP handler that routes by URL path prefix."""

    # Class-level config set by LLMProxyServer before serving
    upstream_map: dict = {}
    ledger_path: str = ""
    capture_logger: Optional[CaptureLogger] = None
    writer: Optional[LedgerWriter] = None

    def do_POST(self):
        body_bytes = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = {}
        try:
            body = json.loads(body_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(400, "Invalid JSON body")
            return

        path = self.path.lower()
        if path.startswith(_OPENAI_PATH):
            self._handle_openai(body)
        elif path.startswith(_ANTHROPIC_PATH):
            self._handle_anthropic(body)
        else:
            self._send_error(404, f"Unknown path: {self.path}")

    def _forward(
        self, upstream_url: str, body: dict, extra_headers: dict = None,
    ) -> Optional[dict]:
        """Forward the request to the upstream and return parsed JSON response.

        Returns None on failure (degradación suave — caller decides action).
        """
        headers = {
            "Content-Type": "application/json",
        }
        for key in self.headers:
            if key.lower() in ("content-type", "content-length", "host"):
                continue
            headers[key] = self.headers[key]
        if extra_headers:
            headers.update(extra_headers)

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            upstream_url, data=data, headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def _send_json(self, status: int, data: dict):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def _handle_openai(self, body: dict):
        model = body.get("model", "unknown")
        prompt = _extract_openai_prompt(body)

        # Determine upstream URL
        upstream_base = self.upstream_map.get("openai", "https://api.openai.com")
        upstream_url = f"{upstream_base}/v1/chat/completions"

        resp = self._forward(upstream_url, body)

        if resp is None:
            # Degradación suave: log what we can, return 502
            self._send_error(502, "Upstream request failed")
            self._log_capture(model, prompt, "", "", 0, 0)
            return

        response_text, reasoning, tokens_in, tokens_out = _extract_openai_response(resp)

        self._send_json(200, resp)
        self._log_capture(model, prompt, response_text, reasoning, tokens_in, tokens_out)

    def _handle_anthropic(self, body: dict):
        model = body.get("model", "unknown")
        prompt = _extract_anthropic_prompt(body)

        upstream_base = self.upstream_map.get("anthropic", "https://api.anthropic.com")
        upstream_url = f"{upstream_base}/v1/messages"

        resp = self._forward(upstream_url, body)

        if resp is None:
            self._send_error(502, "Upstream request failed")
            self._log_capture(model, prompt, "", "", 0, 0)
            return

        response_text, reasoning, tokens_in, tokens_out = _extract_anthropic_response(resp)

        self._send_json(200, resp)
        self._log_capture(model, prompt, response_text, reasoning, tokens_in, tokens_out)

    def _log_capture(
        self,
        model: str,
        prompt: str,
        response_text: str,
        reasoning: str,
        tokens_in: int,
        tokens_out: int,
    ):
        """Log to both capture file and CausaDB ledger (degradación suave)."""
        cost = _calculate_cost(model, tokens_in, tokens_out)

        # Capture file
        entry = {
            "ts_ms": int(time.time() * 1000),
            "model": model,
            "prompt": prompt,
            "response_text": response_text,
            "reasoning": reasoning,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost, 6),
        }
        try:
            if self.capture_logger is not None:
                self.capture_logger.log(entry)
        except Exception:
            pass

        # Ledger: LLM_INVOKED
        try:
            if self.writer is not None:
                event = CanonicalEvent(
                    event_type=EventType.LLM_INVOKED,
                    ctx_id="proxy",
                    source="causadb:proxy-server",
                    source_type="agent",
                    payload=MappingProxyType({
                        "model": model,
                        "prompt": prompt[:2000],
                        "response_text": response_text[:2000],
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "cost_usd": round(cost, 6),
                    }),
                )
                self.writer.append(event)
        except Exception:
            pass

        # Ledger: REASONING_STEP
        if reasoning:
            try:
                if self.writer is not None:
                    event = CanonicalEvent(
                        event_type=EventType.REASONING_STEP,
                        ctx_id="proxy",
                        source="causadb:proxy-server",
                        source_type="agent",
                        payload=MappingProxyType({
                            "model": model,
                            "reasoning": reasoning[:5000],
                        }),
                    )
                    self.writer.append(event)
            except Exception:
                pass

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# LLMProxyServer — public API
# ---------------------------------------------------------------------------

class LLMProxyServer:
    """Local HTTP proxy that captures LLM API calls to the CausaDB ledger.

    Routes OpenAI-compatible and Anthropic-compatible requests, captures
    prompts, responses, reasoning, tokens and cost, and logs both
    LLM_INVOKED and REASONING_STEP events.

    Attributes
    ----------
    host : str
        Bind address (default ``127.0.0.1``).
    port : int
        Bind port (default ``4242``).
    openai_upstream : str
        Upstream base URL for OpenAI-style requests.
    anthropic_upstream : str
        Upstream base URL for Anthropic-style requests.
    capture_path : str or None
        Path for the JSONL capture file. If None, no file capture.
    ledger_path : str or None
        Path to the CausaDB ledger. If None, no ledger logging.
    """

    def __init__(
        self,
        ledger_path: Optional[str] = None,
        host: str = "127.0.0.1",
        port: int = 4242,
        openai_upstream: str = "https://api.openai.com",
        anthropic_upstream: str = "https://api.anthropic.com",
        capture_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.openai_upstream = openai_upstream
        self.anthropic_upstream = anthropic_upstream
        self.capture_path = capture_path
        self.ledger_path = ledger_path

        # Configure handler class variables
        _ProxyHTTPHandler.upstream_map = {
            "openai": openai_upstream,
            "anthropic": anthropic_upstream,
        }
        _ProxyHTTPHandler.ledger_path = ledger_path or ""
        if ledger_path:
            _ProxyHTTPHandler.writer = LedgerWriter(ledger_path)
        else:
            _ProxyHTTPHandler.writer = None
        if capture_path:
            _ProxyHTTPHandler.capture_logger = CaptureLogger(capture_path)
        else:
            _ProxyHTTPHandler.capture_logger = None

        self._server = http.server.HTTPServer(
            (host, port), _ProxyHTTPHandler,
        )

    def start(self):
        """Serve forever (blocks until ``stop()`` is called from another thread)."""
        self._server.serve_forever()

    def stop(self):
        """Shutdown the server (callable from any thread)."""
        self._server.shutdown()