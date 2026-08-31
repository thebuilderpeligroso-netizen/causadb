"""F.3.3 — Modo Proxy de CausaDB.

NOT a MITM proxy. A **client library** wrapping urllib.request (stdlib) to call
LLM APIs and log every invocation as LLM_INVOKED to the CausaDB ledger.

Design (duck typing, no BaseAdapter — Article VIII):
  - OpenAIAdapter.call(model, prompt, api_key, **kwargs) -> dict
  - AnthropicAdapter.call(model, prompt, api_key, **kwargs) -> dict
  - LLMProxy wraps both and auto-logs to LedgerWriter.

Pricing is downloaded with urllib from a public URL and cached locally.
"""

import json
import os
import urllib.request
from types import MappingProxyType
from urllib.error import HTTPError

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter


# ---------------------------------------------------------------------------
# Adapters (concrete only — no BaseAdapter, Article VIII)
# ---------------------------------------------------------------------------

class OpenAIAdapter:
    """OpenAI /chat/completions adapter using urllib (stdlib)."""

    def call(self, model: str, prompt: str, api_key: str, **kwargs) -> dict:
        data = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            **kwargs,
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())

        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        return {
            "content": content,
            "response_tokens": usage.get("total_tokens", 0),
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
        }


class AnthropicAdapter:
    """Anthropic /messages adapter using urllib (stdlib)."""

    def call(self, model: str, prompt: str, api_key: str, **kwargs) -> dict:
        data = json.dumps({
            "model": model,
            "max_tokens": kwargs.get("max_tokens", 1024),
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())

        content = "".join(
            block["text"]
            for block in result.get("content", [])
            if block["type"] == "text"
        )
        usage = result.get("usage", {})
        return {
            "content": content,
            "response_tokens": usage.get("output_tokens", 0) + usage.get("input_tokens", 0),
            "tokens_in": usage.get("input_tokens", 0),
            "tokens_out": usage.get("output_tokens", 0),
        }


class OpenAICompatibleAdapter:
    """Adapter for any server exposing /v1/chat/completions (Ollama, LM Studio, etc.).

    Duck-typed: implements .call(model, prompt, api_key, **kwargs) -> dict
    matching OpenAIAdapter and AnthropicAdapter protocol.
    """

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    def call(self, model: str, prompt: str, api_key: str = "", **kwargs) -> dict:
        data = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            **kwargs,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"]["content"]
        usage = result.get("usage", {})
        return {
            "content": content,
            "response_tokens": usage.get("total_tokens", 0),
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
        }


# ---------------------------------------------------------------------------
# Adapter registry (duck-typed, no base class)
# ---------------------------------------------------------------------------

ADAPTERS = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
    "ollama": OpenAICompatibleAdapter("http://localhost:11434"),
    "lmstudio": OpenAICompatibleAdapter("http://localhost:1234"),
}

# Local (no api_key required) adapters — checked in LLMProxy.call_llm()
LOCAL_ADAPTERS = {"ollama", "lmstudio"}


# ---------------------------------------------------------------------------
# Pricing helper (urllib-based download + local cache)
# ---------------------------------------------------------------------------

def _load_pricing(url: str, cache_path: str) -> dict:
    """Download pricing JSON from *url* and cache at *cache_path*.

    If *cache_path* already exists, read from cache instead of downloading.
    Returns the parsed pricing dict.
    """
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)
    with urllib.request.urlopen(url) as resp:
        pricing = json.loads(resp.read().decode())
    with open(cache_path, "w") as f:
        json.dump(pricing, f)
    return pricing


# ---------------------------------------------------------------------------
# LLMProxy — public API
# ---------------------------------------------------------------------------

class LLMProxy:
    """Call LLM providers via stdlib urllib and auto-log LLM_INVOKED events.

    Usage::

        proxy = LLMProxy(ledger_path="/path/to/ledger.log", api_key="sk-...")
        reply = proxy.call_llm(model="gpt-4", prompt="Hello!", adapter="openai")
    """

    def __init__(
        self,
        ledger_path: str,
        api_key: str = None,
        pricing_cache_path: str = None,
    ):
        self.ledger_path = ledger_path
        self.api_key = api_key
        self.pricing_cache_path = pricing_cache_path
        self._writer = LedgerWriter(ledger_path)

    def call_llm(
        self,
        model: str,
        prompt: str,
        adapter: str = "openai",
        **kwargs,
    ) -> str:
        """Call *model* with *prompt* via *adapter*; log LLM_INVOKED to ledger.

        Returns the response text on success.
        Raises :class:`ValueError` for unknown adapters or missing api_key.
        Propagates HTTP errors from the upstream API.
        """
        if adapter not in ADAPTERS:
            raise ValueError(
                f"Unknown adapter: {adapter}. Available: {list(ADAPTERS.keys())}"
            )
        if not self.api_key and adapter not in LOCAL_ADAPTERS:
            raise ValueError(
                f"api_key is required for adapter '{adapter}'"
            )

        import time
        start = time.time()
        error = None
        result = None

        try:
            result = ADAPTERS[adapter].call(model, prompt, self.api_key, **kwargs)
            content = result["content"]
        except Exception as e:
            error = str(e)
            content = ""
            raise
        finally:
            duration_ms = int((time.time() - start) * 1000)
            payload = MappingProxyType({
                "model": model,
                "prompt": prompt,
                "response_tokens": result.get("response_tokens", 0)
                if not error and result is not None
                else 0,
                "duration_ms": duration_ms,
                "error": error,
            })
            event = CanonicalEvent(
                event_type=EventType.LLM_INVOKED,
                ctx_id="proxy",
                source="causadb:proxy",
                source_type="agent",
                payload=payload,
            )
            self._writer.append(event)

        return content
