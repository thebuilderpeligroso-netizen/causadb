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
import hmac
import json
import os
import re
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

# Spec 1 — dos listas distintas.
# FORWARD: únicos headers que salen al upstream (+ auth condicional, ver _forward).
_FORWARD_HEADERS = frozenset({
    "accept", "accept-language", "anthropic-version", "x-request-id", "user-agent",
})
# NEVER_LOG: jamás escribir headers a ledger/capture (no hay lista de reenvío
# de headers hacia los sumideros — _log_capture nunca recibe headers).
_AUTH_HEADER_NAMES = frozenset({"authorization", "x-api-key"})

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _server_key_for(provider: str) -> Optional[str]:
    """Clave server-side desde env (inyectar y NO reenviar la del cliente)."""
    try:
        if provider == "anthropic":
            return (
                os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_KEY")
                or None
            )
        return (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENAI_KEY")
            or None
        )
    except Exception:
        return None


def _redact_text(value: str) -> str:
    """Redacta secretos con patrones existentes; degradación suave (nunca tumba)."""
    try:
        if not isinstance(value, str) or not value:
            return value
        try:
            from causadb._redactor import _redact_str_value as _rv

            out = _rv(value)
        except Exception:
            out = value
        try:
            # x-api-key no existe en _redactor: tapar valor tras la clave.
            out = re.sub(
                r"(?i)(x-api-key\s*[:=]\s*['\"]?)([^\s'\";,}]+)",
                r"\1***",
                out,
            )
        except Exception:
            pass
        return out
    except Exception:
        try:
            return value  # type: ignore[return-value]
        except Exception:
            return ""

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
            dirpath = os.path.dirname(self.path) or "."
            if not self._dir_ensured:
                try:
                    os.makedirs(dirpath, mode=0o700, exist_ok=True)
                except Exception:
                    pass
                try:
                    if dirpath not in (".", ""):
                        os.chmod(dirpath, 0o700)
                except Exception:
                    pass
                self._dir_ensured = True
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            data = line.encode("utf-8")
            flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
            try:
                fd = os.open(self.path, flags, 0o600)
            except Exception:
                return
            try:
                try:
                    os.chmod(self.path, 0o600)
                except Exception:
                    pass
                os.write(fd, data)
                try:
                    os.fsync(fd)
                except Exception:
                    pass
            finally:
                try:
                    os.close(fd)
                except Exception:
                    pass
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
    require_token: Optional[str] = None

    def do_POST(self):
        # Spec 3 — require_token opcional con compare_digest + 401.
        try:
            expected = getattr(self, "require_token", None)
        except Exception:
            expected = None
        if expected:
            try:
                provided = self.headers.get("X-Proxy-Token")
            except Exception:
                provided = None
            try:
                ok = hmac.compare_digest(str(provided or ""), str(expected))
            except Exception:
                ok = False
            if not ok:
                try:
                    self._send_error(401, "Unauthorized")
                except Exception:
                    pass
                return
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

        Solo lista FORWARD sale al upstream. Auth del cliente SOLO si no hay
        clave server-side; si hay env con clave del servidor, se inyecta esa
        y NO se reenvía la del cliente. Returns None on failure.
        """
        headers = {
            "Content-Type": "application/json",
        }
        try:
            for key in self.headers:
                if key.lower() in _FORWARD_HEADERS:
                    headers[key] = self.headers[key]
        except Exception:
            pass
        # Proveedor por URL (para elegir env de clave server-side).
        try:
            low = (upstream_url or "").lower()
            provider = "anthropic" if ("anthropic" in low or "/messages" in low) else "openai"
        except Exception:
            provider = "openai"
        try:
            server_key = _server_key_for(provider)
        except Exception:
            server_key = None
        try:
            if server_key:
                if provider == "anthropic":
                    headers["x-api-key"] = server_key
                else:
                    headers["Authorization"] = f"Bearer {server_key}"
            else:
                for key in self.headers:
                    try:
                        if key.lower() in _AUTH_HEADER_NAMES:
                            headers[key] = self.headers[key]
                    except Exception:
                        continue
        except Exception:
            pass
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
        """Log to both capture file and CausaDB ledger (degradación suave).

        Spec 2: redact PRIMERO con patrones existentes, truncar DESPUÉS;
        en los 3 campos (prompt/response/reasoning) y 2 sumideros
        (capture + ledger). Tokens/costos intactos. NEVER_LOG: headers jamás.
        """
        cost = _calculate_cost(model, tokens_in, tokens_out)

        # Redact PRIMERO (degradación suave: redact nunca tumba el request).
        try:
            prompt_r = _redact_text(prompt)
        except Exception:
            prompt_r = prompt
        try:
            response_r = _redact_text(response_text)
        except Exception:
            response_r = response_text
        try:
            reasoning_r = _redact_text(reasoning)
        except Exception:
            reasoning_r = reasoning

        # Truncar DESPUÉS (mismo corte en ambos sumideros).
        try:
            prompt_t = prompt_r[:2000] if isinstance(prompt_r, str) else prompt_r
            response_t = response_r[:2000] if isinstance(response_r, str) else response_r
            reasoning_t = reasoning_r[:5000] if isinstance(reasoning_r, str) else reasoning_r
        except Exception:
            prompt_t, response_t, reasoning_t = prompt, response_text, reasoning

        # Capture file
        entry = {
            "ts_ms": int(time.time() * 1000),
            "model": model,
            "prompt": prompt_t,
            "response_text": response_t,
            "reasoning": reasoning_t,
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
                        "prompt": prompt_t,
                        "response_text": response_t,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "cost_usd": round(cost, 6),
                    }),
                )
                self.writer.append(event)
        except Exception:
            pass

        # Ledger: REASONING_STEP
        if reasoning_r:
            try:
                if self.writer is not None:
                    event = CanonicalEvent(
                        event_type=EventType.REASONING_STEP,
                        ctx_id="proxy",
                        source="causadb:proxy-server",
                        source_type="agent",
                        payload=MappingProxyType({
                            "model": model,
                            "reasoning": reasoning_t,
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
        allow_remote: bool = False,
        require_token: Optional[str] = None,
    ):
        # Spec 3 — bind loopback por defecto; allow_remote=True explícito.
        try:
            if not allow_remote and host not in _LOOPBACK_HOSTS:
                host = "127.0.0.1"
        except Exception:
            host = "127.0.0.1"
        self.host = host
        self.port = port
        self.openai_upstream = openai_upstream
        self.anthropic_upstream = anthropic_upstream
        self.capture_path = capture_path
        self.ledger_path = ledger_path
        self.allow_remote = allow_remote
        self.require_token = require_token

        # Configure handler class variables
        _ProxyHTTPHandler.upstream_map = {
            "openai": openai_upstream,
            "anthropic": anthropic_upstream,
        }
        _ProxyHTTPHandler.ledger_path = ledger_path or ""
        _ProxyHTTPHandler.require_token = require_token
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