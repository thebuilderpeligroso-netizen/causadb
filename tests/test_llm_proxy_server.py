"""P.1 — Tests for _llm_proxy_server.py — LLM Capture Proxy.

Tests run against a mock upstream to avoid real API calls.
Verifies: routing, prompt extraction, reasoning capture, cost calculation,
degradación suave, thread lifecycle.
"""

import json
import os
import threading
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_ledger_events(ledger_path: str):
    """Read all CanonicalEvent entries from the hash-chain ledger."""
    events = []
    with open(ledger_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                events.append(entry["event"])
            except (json.JSONDecodeError, KeyError):
                pass
    return events


# ---------------------------------------------------------------------------
# Prompt extraction
# ---------------------------------------------------------------------------

class TestPromptExtraction:
    def test_extract_openai_simple_prompt(self):
        from causadb._llm_proxy_server import _extract_openai_prompt
        body = {"messages": [{"role": "user", "content": "Hello"}]}
        assert _extract_openai_prompt(body) == "Hello"

    def test_extract_openai_multiple_messages(self):
        from causadb._llm_proxy_server import _extract_openai_prompt
        body = {
            "messages": [
                {"role": "system", "content": "Be helpful"},
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "Second"},
            ]
        }
        assert _extract_openai_prompt(body) == "First\nSecond"

    def test_extract_openai_content_list(self):
        from causadb._llm_proxy_server import _extract_openai_prompt
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                }
            ]
        }
        assert _extract_openai_prompt(body) == "Hello"

    def test_extract_openai_empty_messages(self):
        from causadb._llm_proxy_server import _extract_openai_prompt
        assert _extract_openai_prompt({}) == ""
        assert _extract_openai_prompt({"messages": []}) == ""

    def test_extract_anthropic_simple_prompt(self):
        from causadb._llm_proxy_server import _extract_anthropic_prompt
        body = {"messages": [{"role": "user", "content": "Hello"}]}
        assert _extract_anthropic_prompt(body) == "Hello"

    def test_extract_anthropic_content_list(self):
        from causadb._llm_proxy_server import _extract_anthropic_prompt
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello there"},
                    ],
                }
            ]
        }
        assert _extract_anthropic_prompt(body) == "Hello there"


# ---------------------------------------------------------------------------
# Response extraction
# ---------------------------------------------------------------------------

class TestResponseExtraction:
    def test_extract_openai_response_with_reasoning(self):
        from causadb._llm_proxy_server import _extract_openai_response
        body = {
            "choices": [{
                "message": {
                    "content": "Final answer",
                    "reasoning_content": "Step by step reasoning",
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
        text, reasoning, tin, tout = _extract_openai_response(body)
        assert text == "Final answer"
        assert reasoning == "Step by step reasoning"
        assert tin == 10
        assert tout == 20

    def test_extract_openai_response_no_reasoning(self):
        from causadb._llm_proxy_server import _extract_openai_response
        body = {
            "choices": [{"message": {"content": "Just answer"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        text, reasoning, tin, tout = _extract_openai_response(body)
        assert text == "Just answer"
        assert reasoning == ""
        assert tin == 5
        assert tout == 10

    def test_extract_openai_response_empty(self):
        from causadb._llm_proxy_server import _extract_openai_response
        text, reasoning, tin, tout = _extract_openai_response({})
        assert text == ""
        assert reasoning == ""

    def test_extract_anthropic_response_with_thinking(self):
        from causadb._llm_proxy_server import _extract_anthropic_response
        body = {
            "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "The answer is 42."},
            ],
            "usage": {"input_tokens": 15, "output_tokens": 25},
        }
        text, reasoning, tin, tout = _extract_anthropic_response(body)
        assert text == "The answer is 42."
        assert reasoning == "Let me think..."
        assert tin == 15
        assert tout == 25

    def test_extract_anthropic_response_no_thinking(self):
        from causadb._llm_proxy_server import _extract_anthropic_response
        body = {
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }
        text, reasoning, tin, tout = _extract_anthropic_response(body)
        assert text == "Hello"
        assert reasoning == ""
        assert tin == 5
        assert tout == 10

    def test_extract_anthropic_response_multiple_thinking_blocks(self):
        """Multiple thinking blocks should be concatenated, not overwritten."""
        from causadb._llm_proxy_server import _extract_anthropic_response
        body = {
            "content": [
                {"type": "thinking", "thinking": "First thought"},
                {"type": "thinking", "thinking": "Second thought"},
                {"type": "thinking", "thinking": "Third thought"},
                {"type": "text", "text": "Final answer"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 10},
        }
        text, reasoning, tin, tout = _extract_anthropic_response(body)
        assert text == "Final answer"
        assert reasoning == "First thought\nSecond thought\nThird thought"

    def test_extract_anthropic_response_empty(self):
        from causadb._llm_proxy_server import _extract_anthropic_response
        text, reasoning, tin, tout = _extract_anthropic_response({})
        assert text == ""
        assert reasoning == ""


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

class TestCostCalculation:
    def test_cost_gpt4o(self):
        from causadb._llm_proxy_server import _calculate_cost
        cost = _calculate_cost("gpt-4o", 1000, 500)
        assert cost == pytest.approx(1000 * 2.5 / 1e6 + 500 * 10.0 / 1e6)

    def test_cost_claude_sonnet(self):
        from causadb._llm_proxy_server import _calculate_cost
        cost = _calculate_cost("claude-3-5-sonnet-20241022", 1000, 500)
        assert cost == pytest.approx(1000 * 3.0 / 1e6 + 500 * 15.0 / 1e6)

    def test_cost_claude_opus(self):
        from causadb._llm_proxy_server import _calculate_cost
        cost = _calculate_cost("claude-3-opus-20240229", 1000, 500)
        assert cost == pytest.approx(1000 * 15.0 / 1e6 + 500 * 75.0 / 1e6)

    def test_cost_unknown_model(self):
        from causadb._llm_proxy_server import _calculate_cost
        assert _calculate_cost("unknown-model", 1000, 500) == 0.0

    def test_cost_zero_tokens(self):
        from causadb._llm_proxy_server import _calculate_cost
        assert _calculate_cost("gpt-4o", 0, 0) == 0.0


# ---------------------------------------------------------------------------
# CaptureLogger
# ---------------------------------------------------------------------------

class TestCaptureLogger:
    def test_log_writes_jsonl(self, tmp_path):
        from causadb._llm_proxy_server import CaptureLogger
        path = os.path.join(tmp_path, "capture.jsonl")
        logger = CaptureLogger(path)
        logger.log({"model": "gpt-4", "tokens_in": 10})
        logger.log({"model": "claude-3", "tokens_in": 20})
        lines = open(path).read().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["model"] == "gpt-4"
        assert json.loads(lines[1])["model"] == "claude-3"

    def test_log_creates_dir(self, tmp_path):
        from causadb._llm_proxy_server import CaptureLogger
        path = os.path.join(tmp_path, "sub", "dir", "capture.jsonl")
        logger = CaptureLogger(path)
        logger.log({"test": True})
        assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Full proxy integration test
# ---------------------------------------------------------------------------

class TestLLMProxyServerIntegration:
    """Start a real LLMProxyServer + mock upstream, send requests, verify ledger."""

    def test_proxy_forwards_openai_request(self, tmp_path):
        """Start server, send OpenAI request, verify response and ledger entries."""
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")

        # Mock upstream that returns a response with reasoning_content
        mock_upstream_body = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4o",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "The answer is 42.",
                    "reasoning_content": "Step 1: parse question.\nStep 2: compute.\nStep 3: answer 42.",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        }

        # We'll mock urllib.request.urlopen inside _forward
        # Actually, let's create a simple mock HTTP server. Simpler: patch _forward
        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(
            ledger_path=ledger,
            host="127.0.0.1",
            port=0,  # OS-assigned port
            openai_upstream="http://127.0.0.1:9999",  # won't be called
            anthropic_upstream="http://127.0.0.1:9999",
            capture_path=capture,
        )
        port = server._server.server_address[1]

        # Start in a thread
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            # Mock _forward to return our mock response
            handler_cls = server._server.RequestHandlerClass
            with patch.object(
                handler_cls,
                "_forward",
                return_value=mock_upstream_body,
            ):
                import urllib.request as ur

                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "What is the answer?"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
                )
                with ur.urlopen(req) as resp:
                    result = json.loads(resp.read().decode())

            # Verify response passed through
            assert result["choices"][0]["message"]["content"] == "The answer is 42."
            assert result["choices"][0]["message"]["reasoning_content"] is not None

            # Wait for async logging
            time.sleep(0.2)

            # Verify ledger has LLM_INVOKED
            events = _read_ledger_events(ledger)
            llm_events = [e for e in events if e["event_type"] == "LLM_INVOKED"]
            assert len(llm_events) >= 1
            assert llm_events[0]["payload"]["model"] == "gpt-4o"

            # Verify ledger has REASONING_STEP
            reason_events = [e for e in events if e["event_type"] == "REASONING_STEP"]
            assert len(reason_events) >= 1
            assert "Step 1" in reason_events[0]["payload"]["reasoning"]

            # Verify capture file
            cap_lines = open(capture).read().strip().split("\n")
            assert len(cap_lines) >= 1
            cap_entry = json.loads(cap_lines[0])
            assert cap_entry["model"] == "gpt-4o"
            assert cap_entry["reasoning"] == mock_upstream_body["choices"][0]["message"]["reasoning_content"]

        finally:
            server.stop()
            t.join(timeout=3)

    def test_proxy_forwards_anthropic_request(self, tmp_path):
        """Send Anthropic request, verify reasoning capture from thinking block."""
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")

        mock_upstream_body = {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me calculate..."},
                {"type": "text", "text": "The answer is 42."},
            ],
            "model": "claude-3-opus-20240229",
            "usage": {"input_tokens": 40, "output_tokens": 20},
        }

        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(
            ledger_path=ledger,
            host="127.0.0.1",
            port=0,
            openai_upstream="http://127.0.0.1:9999",
            anthropic_upstream="http://127.0.0.1:9999",
            capture_path=capture,
        )
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(
                handler_cls,
                "_forward",
                return_value=mock_upstream_body,
            ):
                import urllib.request as ur

                data = json.dumps({
                    "model": "claude-3-opus-20240229",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "What is 2+2?"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/anthropic/v1/messages",
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": "sk-test",
                        "anthropic-version": "2023-06-01",
                    },
                )
                with ur.urlopen(req) as resp:
                    result = json.loads(resp.read().decode())

            assert result["content"][1]["text"] == "The answer is 42."
            time.sleep(0.2)

            events = _read_ledger_events(ledger)
            reason_events = [e for e in events if e["event_type"] == "REASONING_STEP"]
            assert len(reason_events) >= 1
            assert "Let me calculate" in reason_events[0]["payload"]["reasoning"]

            cap_lines = open(capture).read().strip().split("\n")
            assert len(cap_lines) >= 1
            cap_entry = json.loads(cap_lines[0])
            assert cap_entry["reasoning"] == "Let me calculate..."

        finally:
            server.stop()
            t.join(timeout=3)

    def test_proxy_unknown_path_returns_404(self, tmp_path):
        """Unknown paths should return 404 without crashing."""
        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(host="127.0.0.1", port=0, capture_path=os.path.join(tmp_path, "cap.jsonl"))
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            import urllib.request as ur
            import urllib.error

            data = json.dumps({"model": "gpt-4"}).encode()
            req = ur.Request(
                f"http://127.0.0.1:{port}/unknown/path",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(ur.HTTPError) as exc:
                ur.urlopen(req)
            assert exc.value.code == 404
        finally:
            server.stop()
            t.join(timeout=3)

    def test_proxy_degradacion_suave_on_upstream_fail(self, tmp_path):
        """When upstream fails, proxy returns 502 and does NOT crash."""
        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(
            ledger_path=os.path.join(tmp_path, "ledger.log"),
            host="127.0.0.1",
            port=0,
            openai_upstream="http://127.0.0.1:1",  # non-routable
        )
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            import urllib.request as ur
            import urllib.error

            data = json.dumps({
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            req = ur.Request(
                f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
            )
            with pytest.raises(ur.HTTPError) as exc:
                ur.urlopen(req)
            assert exc.value.code == 502
        finally:
            server.stop()
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# Degradación suave on malformed JSON
# ---------------------------------------------------------------------------

class TestDegradacionSuave:
    def test_invalid_json_returns_400(self, tmp_path):
        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(host="127.0.0.1", port=0)
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            import urllib.request as ur
            import urllib.error

            req = ur.Request(
                f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(ur.HTTPError) as exc:
                ur.urlopen(req)
            assert exc.value.code == 400
        finally:
            server.stop()
            t.join(timeout=3)

    def test_proxy_logging_error_does_not_crash(self, tmp_path):
        """If capture file is unwritable, server should still respond normally."""
        ledger = os.path.join(tmp_path, "ledger.log")
        # Create a FILE where a directory is expected — os.makedirs will fail
        blocker = os.path.join(tmp_path, "blocker")
        open(blocker, "w").close()
        capture = os.path.join(blocker, "capture.jsonl")  # parent is a FILE, not a dir

        mock_body = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }

        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(
            ledger_path=ledger,
            host="127.0.0.1",
            port=0,
            capture_path=capture,
        )
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(
                handler_cls,
                "_forward",
                return_value=mock_body,
            ):
                import urllib.request as ur

                data = json.dumps({
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
        finally:
            server.stop()
            t.join(timeout=3)


# ---------------------------------------------------------------------------
# No reasoning → no REASONING_STEP
# ---------------------------------------------------------------------------

class TestNoReasoning:
    def test_no_reasoning_no_reasoning_step(self, tmp_path):
        """If no reasoning_content, no REASONING_STEP event should be emitted."""
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")

        mock_body = {
            "choices": [{"message": {"content": "Just answer"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }

        from causadb._llm_proxy_server import LLMProxyServer

        server = LLMProxyServer(
            ledger_path=ledger,
            host="127.0.0.1",
            port=0,
            capture_path=capture,
        )
        port = server._server.server_address[1]

        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(
                handler_cls,
                "_forward",
                return_value=mock_body,
            ):
                import urllib.request as ur

                data = json.dumps({
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": "hello"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200

            time.sleep(0.2)
            events = _read_ledger_events(ledger)
            reason_events = [e for e in events if e["event_type"] == "REASONING_STEP"]
            assert len(reason_events) == 0
        finally:
            server.stop()
            t.join(timeout=3)