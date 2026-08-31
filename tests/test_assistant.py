"""Tests for _assistant.py — causal ledger Ollama assistant.

Artículo III: test-first. Artículo IX: Fall-Closed on errors.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from causadb._assistant import Assistant, AssistantError, SYSTEM_PROMPT


def test_assistant_init():
    a = Assistant()
    assert a.ollama_url == "http://127.0.0.1:11434"
    assert a.model == "smollm2:135m"


def test_assistant_custom_url():
    a = Assistant(ollama_url="http://10.0.0.1:11434", model="llama3.2:1b")
    assert a.ollama_url == "http://10.0.0.1:11434"
    assert a.model == "llama3.2:1b"


@patch("urllib.request.urlopen")
def test_assistant_ask_returns_response(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.status = 200
    mock_resp.__enter__.return_value.read.return_value = json.dumps(
        {"response": "Hola! Soy el asistente."}
    ).encode()
    mock_urlopen.return_value = mock_resp

    a = Assistant()
    response = a.ask("Hola")
    assert response == "Hola! Soy el asistente."


@patch("urllib.request.urlopen")
def test_assistant_ask_raises_on_http_error(mock_urlopen):
    from urllib.error import HTTPError

    mock_urlopen.side_effect = HTTPError(
        "/api/generate", 500, "Internal Server Error", {}, None
    )

    a = Assistant()
    with pytest.raises(AssistantError, match="Ollama HTTP error"):
        a.ask("test")


@patch("urllib.request.urlopen")
def test_assistant_ask_raises_on_connection_error(mock_urlopen):
    from urllib.error import URLError

    mock_urlopen.side_effect = URLError("Connection refused")

    a = Assistant()
    with pytest.raises(AssistantError, match="Ollama connection error"):
        a.ask("test")


@patch("urllib.request.urlopen")
def test_is_ollama_running_returns_true(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.status = 200
    mock_urlopen.return_value = mock_resp

    assert Assistant.is_ollama_running() is True


@patch("urllib.request.urlopen")
def test_is_ollama_running_returns_false(mock_urlopen):
    mock_urlopen.side_effect = Exception("Connection refused")
    assert Assistant.is_ollama_running() is False


@patch("urllib.request.urlopen")
def test_list_models(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.status = 200
    mock_resp.__enter__.return_value.read.return_value = json.dumps({
        "models": [{"name": "smollm2:135m"}, {"name": "llama3.2:1b"}]
    }).encode()
    mock_urlopen.return_value = mock_resp

    models = Assistant.list_models()
    assert "smollm2:135m" in models
    assert "llama3.2:1b" in models


@patch("urllib.request.urlopen")
def test_pull_model(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value.status = 200
    mock_urlopen.return_value = mock_resp

    assert Assistant.pull_model("smollm2:135m") is True


@patch("urllib.request.urlopen")
def test_pull_model_fails(mock_urlopen):
    mock_urlopen.side_effect = Exception("Connection refused")
    assert Assistant.pull_model("smollm2:135m") is False


@patch("urllib.request.urlopen")
def test_list_models_fails_gracefully(mock_urlopen):
    mock_urlopen.side_effect = Exception("Timeout")
    models = Assistant.list_models()
    assert models == []


def test_system_prompt_contains_key_info():
    """Verify system prompt has CausaDB-specific knowledge."""
    assert "causadb init" in SYSTEM_PROMPT
    assert "causadb replay" in SYSTEM_PROMPT
    assert "Score" in SYSTEM_PROMPT
    assert "127.0.0.1:7457" in SYSTEM_PROMPT
    assert "causadb serve" in SYSTEM_PROMPT
    assert "causadb query" in SYSTEM_PROMPT
    assert "causadb validate" in SYSTEM_PROMPT
    assert "causadb bisect" in SYSTEM_PROMPT
    assert "causadb audit" in SYSTEM_PROMPT
