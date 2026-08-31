"""Local assistant using Ollama. 100% offline, no internet required."""

import json
import os
import urllib.request
import urllib.error
import logging

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = os.environ.get("CAUSADB_OLLAMA_MODEL", "smollm2:135m")

# CausaDB-specialized system prompt
SYSTEM_PROMPT = """Eres un asistente especializado en CausaDB, una herramienta de bitácora para proyectos de IA.

Conoces estos comandos CLI:
- causadb init: inicializa un workspace
- causadb serve: inicia el daemon y dashboard web
- causadb replay: reconstruye el estado del proyecto desde el ledger
- causadb score: muestra el Score de productividad (0-100)
- causadb why archivo.py:42: atribuye una línea de código a su evento
- causadb trace archivo.py:42: muestra el árbol causal de una línea
- causadb impact <event_id>: muestra el impacto downstream de un evento
- causadb query --event-type: filtra eventos por tipo
- causadb validate: verifica la integridad del hash chain
- causadb sentinel: corre reglas de integridad
- causadb update: actualiza CausaDB
- causadb crash list: lista crashes registrados
- causadb telemetry status: estado de telemetría
- causadb config: configuración
- causadb feedback: feedback humano
- causadb bisect --test cmd: búsqueda binaria del evento que rompió un test
- causadb audit: auditoría de sobrevivencia de código IA

El dashboard web corre en http://127.0.0.1:7457 y muestra:
- Timeline de eventos
- Panel de Score (número + barras Churn/Waste/Survival)
- Banner de actualizaciones disponibles
- Banner de crashes sin enviar
- Toggle de telemetría anónima

Respondé preguntas en español, de forma clara y concisa. Si no sabés la respuesta, decí que no sabés."""


class AssistantError(Exception):
    """Base error for assistant operations."""


class Assistant:
    """Local LLM assistant using Ollama."""

    def __init__(self, ollama_url: str = DEFAULT_OLLAMA_URL, model: str = DEFAULT_MODEL):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model

    def _generate(self, prompt: str, max_tokens: int = 512) -> str:
        """Send prompt to Ollama and return response."""
        url = f"{self.ollama_url}/api/generate"
        payload = json.dumps({
            "model": self.model,
            "prompt": f"{SYSTEM_PROMPT}\n\nUsuario: {prompt}\nAsistente:",
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.3,
            }
        }).encode()
        
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                return result.get("response", "").strip()
        except urllib.error.HTTPError as e:
            raise AssistantError(f"Ollama HTTP error: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise AssistantError(f"Ollama connection error: {e.reason}. Is Ollama running?")
        except (json.JSONDecodeError, KeyError) as e:
            raise AssistantError(f"Ollama response parse error: {e}")
        except OSError as e:
            raise AssistantError(f"Network error: {e}")

    def ask(self, question: str) -> str:
        """Ask a question to the assistant."""
        return self._generate(question)

    @staticmethod
    def is_ollama_running(ollama_url: str = DEFAULT_OLLAMA_URL) -> bool:
        """Check if Ollama server is available."""
        url = ollama_url.rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def list_models(ollama_url: str = DEFAULT_OLLAMA_URL) -> list:
        """List available models from Ollama."""
        url = ollama_url.rstrip("/")
        try:
            req = urllib.request.Request(f"{url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    @staticmethod
    def pull_model(model: str = DEFAULT_MODEL, ollama_url: str = DEFAULT_OLLAMA_URL) -> bool:
        """Pull a model from Ollama library."""
        url = ollama_url.rstrip("/")
        payload = json.dumps({"name": model, "stream": False}).encode()
        req = urllib.request.Request(f"{url}/api/pull", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.status == 200
        except Exception:
            return False
