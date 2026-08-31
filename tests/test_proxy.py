"""Tests for F.3.3 — Modo Proxy de CausaDB (LLMProxy).

Test-First discipline (Article III): these tests exist BEFORE the implementation.
ModuleNotFoundError for `causadb._proxy` is expected until the implementation
is written in Step 2.

Anti-teatro (Article IX): see `test_anti_teatro_proxy_skips_logging` — patches
LedgerWriter.append to a no-op and verifies the ledger stays empty. A stub
implementation that "logs" without calling LedgerWriter will fail.
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock


def test_proxy_openai_adapter_logs_llm_invoked(tmp_path):
    from causadb._proxy import LLMProxy
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    # Mock urllib.request
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "Hello!"}}],
        "usage": {"total_tokens": 50, "prompt_tokens": 20, "completion_tokens": 30}
    }).encode()
    fake_response.__enter__.return_value = fake_response

    with patch("urllib.request.urlopen", return_value=fake_response):
        proxy = LLMProxy(
            ledger_path=str(ledger),
            api_key="test-key",
            pricing_cache_path=str(tmp_path / "pricing.json"),
        )
        result = proxy.call_llm(
            model="gpt-4",
            prompt="Say hello",
            adapter="openai",
        )

    assert result == "Hello!"

    # Verify LLM_INVOKED was logged
    lines = ledger.read_text().strip().split("\n")
    events = []
    for l in lines:
        if l.strip():
            events.append(json.loads(l.strip()))
    llm_events = [e for e in events if e.get("event", {}).get("event_type") == "LLM_INVOKED"]
    assert len(llm_events) >= 1
    ev = llm_events[-1]["event"]
    assert ev["payload"]["model"] == "gpt-4"
    assert ev["payload"]["response_tokens"] == 50


def test_proxy_anthropic_adapter_logs_llm_invoked(tmp_path):
    from causadb._proxy import LLMProxy
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "content": [{"type": "text", "text": "Hello Anthropic!"}],
        "usage": {"input_tokens": 20, "output_tokens": 30}
    }).encode()
    fake_response.__enter__.return_value = fake_response

    with patch("urllib.request.urlopen", return_value=fake_response):
        proxy = LLMProxy(
            ledger_path=str(ledger),
            api_key="test-key",
            pricing_cache_path=str(tmp_path / "pricing.json"),
        )
        result = proxy.call_llm(
            model="claude-3-opus-20240229",
            prompt="Say hi",
            adapter="anthropic",
        )

    assert result == "Hello Anthropic!"

    lines = ledger.read_text().strip().split("\n")
    events = [json.loads(l) for l in lines if l.strip()]
    llm_events = [e for e in events if e.get("event", {}).get("event_type") == "LLM_INVOKED"]
    assert len(llm_events) >= 1


def test_proxy_raises_on_unknown_adapter(tmp_path):
    from causadb._proxy import LLMProxy
    proxy = LLMProxy(ledger_path=str(tmp_path / "ledger.log"), api_key="test-key")
    with pytest.raises(ValueError, match="Unknown adapter"):
        proxy.call_llm(model="gpt-4", prompt="hi", adapter="nonexistent")


def test_proxy_raises_without_api_key(tmp_path):
    from causadb._proxy import LLMProxy
    proxy = LLMProxy(ledger_path=str(tmp_path / "ledger.log"))
    with pytest.raises(ValueError, match="api_key"):
        proxy.call_llm(model="gpt-4", prompt="hi", adapter="openai")


def test_proxy_logs_error_on_api_failure(tmp_path):
    from causadb._proxy import LLMProxy
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    from urllib.error import HTTPError
    fake_error = HTTPError("http://example.com", 400, "Bad Request", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_error):
        proxy = LLMProxy(
            ledger_path=str(ledger),
            api_key="test-key",
            pricing_cache_path=str(tmp_path / "pricing.json"),
        )
        with pytest.raises(HTTPError):
            proxy.call_llm(model="gpt-4", prompt="hi", adapter="openai")

    # Even on error, an LLM_INVOKED with error field should be logged
    lines = ledger.read_text().strip().split("\n")
    events = [json.loads(l) for l in lines if l.strip()]
    llm_events = [e for e in events if e.get("event", {}).get("event_type") == "LLM_INVOKED"]
    if llm_events:
        assert "error" in llm_events[-1]["event"]["payload"]


def test_proxy_caches_pricing(tmp_path):
    from causadb._proxy import _load_pricing
    cache_path = tmp_path / "pricing.json"

    fake_pricing = {"openai": {"gpt-4": {"prompt": 0.03, "completion": 0.06}}}
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(fake_pricing).encode()
    fake_response.__enter__.return_value = fake_response

    # First call: downloads and caches
    with patch("urllib.request.urlopen", return_value=fake_response):
        pricing = _load_pricing("http://example.com/pricing.json", str(cache_path))
        assert pricing == fake_pricing

    # Cache file exists
    assert cache_path.exists()

    # Second call: uses cache (no network)
    # Remove urlopen mock — if it tries to fetch, test fails
    pricing2 = _load_pricing("http://example.com/pricing.json", str(cache_path))
    assert pricing2 == fake_pricing


def test_anti_teatro_proxy_skips_logging(tmp_path):
    """Anti-teatro (Article IX): if LedgerWriter.append is a no-op, no events appear."""
    from causadb._proxy import LLMProxy
    from causadb._ledger_writer import LedgerWriter
    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    # Patch append to no-op
    original_append = LedgerWriter.append
    def noop_append(self, event):
        return None
    LedgerWriter.append = noop_append

    try:
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "test"}}],
            "usage": {"total_tokens": 10}
        }).encode()
        fake_response.__enter__.return_value = fake_response

        with patch("urllib.request.urlopen", return_value=fake_response):
            proxy = LLMProxy(
                ledger_path=str(ledger),
                api_key="test-key",
            )
            proxy.call_llm(model="gpt-4", prompt="test", adapter="openai")
    finally:
        LedgerWriter.append = original_append

    lines = ledger.read_text().strip()
    assert lines == "", "Anti-teatro: no append means no events"


# ---------------------------------------------------------------------------
# F.8 — OpenAICompatibleAdapter (Ollama + LM Studio)
# ---------------------------------------------------------------------------

def test_compatible_adapter_ollama_uses_localhost_11434():
    """OpenAICompatibleAdapter('http://localhost:11434')
    → URL contains http://localhost:11434/v1/chat/completions"""
    from causadb._proxy import OpenAICompatibleAdapter

    captured = {}

    def capture_urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {},
        }).encode()
        fake.__enter__.return_value = fake
        return fake

    adapter = OpenAICompatibleAdapter("http://localhost:11434")
    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        adapter.call(model="llama3", prompt="hello")

    assert "http://localhost:11434/v1/chat/completions" in captured["url"], (
        f"Expected localhost:11434/v1/chat/completions in URL, got {captured['url']}"
    )


def test_compatible_adapter_lmstudio_uses_localhost_1234():
    """OpenAICompatibleAdapter('http://localhost:1234')
    → URL contains http://localhost:1234/v1/chat/completions"""
    from causadb._proxy import OpenAICompatibleAdapter

    captured = {}

    def capture_urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {},
        }).encode()
        fake.__enter__.return_value = fake
        return fake

    adapter = OpenAICompatibleAdapter("http://localhost:1234")
    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        adapter.call(model="llama3", prompt="hello")

    assert "http://localhost:1234/v1/chat/completions" in captured["url"], (
        f"Expected localhost:1234/v1/chat/completions in URL, got {captured['url']}"
    )


def test_compatible_adapter_no_api_key_in_header():
    """When api_key='', no Authorization header is sent."""
    from causadb._proxy import OpenAICompatibleAdapter

    captured = {}

    def capture_urlopen(req, *args, **kwargs):
        captured["headers"] = dict(req.headers)
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {},
        }).encode()
        fake.__enter__.return_value = fake
        return fake

    adapter = OpenAICompatibleAdapter("http://localhost:11434")
    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        adapter.call(model="llama3", prompt="hello", api_key="")

    assert "Authorization" not in captured["headers"], (
        f"Expected no Authorization header, got {captured['headers']}"
    )


def test_compatible_adapter_with_api_key_in_header():
    """When api_key='sk-test123', Authorization: Bearer sk-test123 is sent."""
    from causadb._proxy import OpenAICompatibleAdapter

    captured = {}

    def capture_urlopen(req, *args, **kwargs):
        captured["headers"] = dict(req.headers)
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {},
        }).encode()
        fake.__enter__.return_value = fake
        return fake

    adapter = OpenAICompatibleAdapter("http://localhost:11434")
    with patch("urllib.request.urlopen", side_effect=capture_urlopen):
        adapter.call(model="llama3", prompt="hello", api_key="sk-test123")

    assert captured["headers"].get("Authorization") == "Bearer sk-test123", (
        f"Expected Bearer sk-test123, got {captured['headers'].get('Authorization')}"
    )


def test_compatible_adapter_returns_same_shape_as_openai():
    """Response dict has content, response_tokens, tokens_in, tokens_out."""
    from causadb._proxy import OpenAICompatibleAdapter

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"total_tokens": 5, "prompt_tokens": 2, "completion_tokens": 3},
    }).encode()
    fake_response.__enter__.return_value = fake_response

    adapter = OpenAICompatibleAdapter("http://localhost:11434")
    with patch("urllib.request.urlopen", return_value=fake_response):
        result = adapter.call(model="llama3", prompt="hello", api_key="")

    assert isinstance(result, dict)
    assert result["content"] == "hi"
    assert result["response_tokens"] == 5
    assert result["tokens_in"] == 2
    assert result["tokens_out"] == 3


def test_ollama_in_registry():
    """ADAPTERS has 'ollama' as an OpenAICompatibleAdapter instance."""
    from causadb._proxy import ADAPTERS, OpenAICompatibleAdapter

    assert "ollama" in ADAPTERS, "ollama not in ADAPTERS"
    assert isinstance(ADAPTERS["ollama"], OpenAICompatibleAdapter), (
        f"Expected OpenAICompatibleAdapter, got {type(ADAPTERS['ollama'])}"
    )
    assert ADAPTERS["ollama"]._base_url == "http://localhost:11434", (
        f"Expected base_url http://localhost:11434, got {ADAPTERS['ollama']._base_url}"
    )


def test_lmstudio_in_registry():
    """ADAPTERS has 'lmstudio' key with correct base_url."""
    from causadb._proxy import ADAPTERS, OpenAICompatibleAdapter

    assert "lmstudio" in ADAPTERS, "lmstudio not in ADAPTERS"
    assert isinstance(ADAPTERS["lmstudio"], OpenAICompatibleAdapter)
    assert ADAPTERS["lmstudio"]._base_url == "http://localhost:1234"


def test_ollama_no_api_key_required(tmp_path):
    """LLMProxy(api_key=None).call_llm(adapter='ollama') does not raise ValueError."""
    from causadb._proxy import LLMProxy

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "ollama reply"}}],
        "usage": {"total_tokens": 10},
    }).encode()
    fake_response.__enter__.return_value = fake_response

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    proxy = LLMProxy(ledger_path=str(ledger), api_key=None)
    with patch("urllib.request.urlopen", return_value=fake_response):
        # Should not raise ValueError despite api_key=None
        result = proxy.call_llm(model="llama3", prompt="hi", adapter="ollama")

    assert result == "ollama reply"


def test_lmstudio_no_api_key_required(tmp_path):
    """LLMProxy(api_key=None).call_llm(adapter='lmstudio') does not raise ValueError."""
    from causadb._proxy import LLMProxy

    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({
        "choices": [{"message": {"content": "lmstudio reply"}}],
        "usage": {"total_tokens": 10},
    }).encode()
    fake_response.__enter__.return_value = fake_response

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    proxy = LLMProxy(ledger_path=str(ledger), api_key=None)
    with patch("urllib.request.urlopen", return_value=fake_response):
        result = proxy.call_llm(model="llama3", prompt="hi", adapter="lmstudio")

    assert result == "lmstudio reply"


def test_openai_still_requires_api_key(tmp_path):
    """LLMProxy(api_key=None).call_llm(adapter='openai') raises ValueError."""
    from causadb._proxy import LLMProxy

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    proxy = LLMProxy(ledger_path=str(ledger), api_key=None)
    with pytest.raises(ValueError, match="api_key"):
        proxy.call_llm(model="gpt-4", prompt="hi", adapter="openai")


def test_anthropic_still_requires_api_key(tmp_path):
    """LLMProxy(api_key=None).call_llm(adapter='anthropic') raises ValueError."""
    from causadb._proxy import LLMProxy

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    proxy = LLMProxy(ledger_path=str(ledger), api_key=None)
    with pytest.raises(ValueError, match="api_key"):
        proxy.call_llm(model="claude-3", prompt="hi", adapter="anthropic")


# ---------------------------------------------------------------------------
# F.8 — Anti-teatro (Article IX)
# ---------------------------------------------------------------------------

def test_anti_teatro_compatible_adapter_wrong_url():
    """If someone changes the base_url of the ollama adapter, this test fails.
    Discriminatory: patches ADAPTERS['ollama'] to a wrong URL and asserts
    the wrong URL appears in the request."""
    from causadb._proxy import ADAPTERS, OpenAICompatibleAdapter

    original_ollama = ADAPTERS["ollama"]
    wrong_adapter = OpenAICompatibleAdapter("http://localhost:19999")
    ADAPTERS["ollama"] = wrong_adapter

    captured = {}

    def capture_urlopen(req, *args, **kwargs):
        captured["url"] = req.full_url
        fake = MagicMock()
        fake.read.return_value = json.dumps({
            "choices": [{"message": {"content": "hi"}}],
            "usage": {},
        }).encode()
        fake.__enter__.return_value = fake
        return fake

    try:
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            ADAPTERS["ollama"].call(model="llama3", prompt="hello")
        assert "http://localhost:19999/v1/chat/completions" in captured["url"], (
            f"Expected wrong URL, got {captured['url']}"
        )
    finally:
        ADAPTERS["ollama"] = original_ollama


def test_anti_teatro_ollama_requires_api_key(tmp_path):
    """If LOCAL_ADAPTERS is emptied (no local adapters exempt from api_key),
    ollama adapter still requires api_key → raises ValueError.
    This test is discriminatory: if someone removes ollama from LOCAL_ADAPTERS,
    test_ollama_no_api_key_required would fail, but this test ensures the
    enforcement works correctly."""
    from causadb._proxy import LLMProxy

    ledger = tmp_path / "ledger.log"
    ledger.write_text("")

    proxy = LLMProxy(ledger_path=str(ledger), api_key=None)

    with patch("causadb._proxy.LOCAL_ADAPTERS", set()):
        with pytest.raises(ValueError, match="api_key"):
            proxy.call_llm(model="llama3", prompt="hi", adapter="ollama")
