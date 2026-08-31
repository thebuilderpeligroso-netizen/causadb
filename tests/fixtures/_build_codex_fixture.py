"""Builder de la fixture de sesión Codex CLI (Artículo IX: datos reales).

Extrae un fragmento REAL de ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl``
y lo guarda recortado en ``tests/fixtures/codex_rollout_fixture.jsonl``:

  - ``session_meta``: session_id reemplazado por ``fixture-session-001``,
    base_instructions recortado a 200 chars.
  - ``event_msg`` (task_started / task_complete): turn_id reemplazado.
  - ``response_item`` (developer/user): textos recortados a 200 chars.
  - ``turn_context``: preservado con campos largos recortados.
  - ``world_state``: preservado (payload vacío).

Uso:
    .venv/bin/python tests/fixtures/_build_codex_fixture.py
"""

import json
import os
from glob import glob


OUT_PATH = os.path.join(os.path.dirname(__file__), "codex_rollout_fixture.jsonl")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")


def _trim(s: str, limit: int) -> str:
    """Recorta un string largo preservando JSON válido."""
    if s is None:
        return s
    if len(s) <= limit:
        return s
    return s[:limit] + "…[recortado-fixture]"


def _find_latest_rollout() -> str | None:
    """Encuentra el rollout más reciente en ~/.codex/sessions/."""
    pattern = os.path.join(CODEX_SESSIONS_DIR, "*", "*", "*", "rollout-*.jsonl")
    files = sorted(glob(pattern), key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def build(rollout_path: str | None = None) -> str:
    rollout_path = rollout_path or os.environ.get("CODEX_ROLLOUT_PATH") or _find_latest_rollout()
    if not rollout_path:
        raise FileNotFoundError(
            "No se encontró ningún rollout en ~/.codex/sessions/. "
            "Pasá CODEX_ROLLOUT_PATH o el path como argumento."
        )

    with open(rollout_path) as f:
        raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]

    lines: list[str] = []
    for raw_line in raw_lines:
        obj = json.loads(raw_line)
        t = obj["type"]
        p = obj.get("payload", {})

        if t == "session_meta":
            p["session_id"] = "fixture-session-001"
            p["id"] = "fixture-session-001"
            if "base_instructions" in p and isinstance(p["base_instructions"], dict):
                txt = p["base_instructions"].get("text", "")
                p["base_instructions"]["text"] = _trim(txt, 200)

        elif t == "event_msg":
            if p.get("type") in ("task_started", "task_complete", "user_message"):
                p["turn_id"] = "fixture-turn-001"

        elif t == "response_item":
            content = p.get("content", [])
            for c in content:
                if isinstance(c, dict) and "text" in c:
                    c["text"] = _trim(c["text"], 200)

        elif t == "turn_context":
            # Inyectar modelo sintético si el real es None (el harvest
            # necesita un modelo para generar LLM_INVOKED).
            if p.get("selected_model") is None:
                p["selected_model"] = "gpt-5.6-sol"
            for k in list(p.keys()):
                if isinstance(p[k], str) and len(p[k]) > 200:
                    p[k] = _trim(p[k], 200)

        # world_state: preservar tal cual

        lines.append(json.dumps(obj, ensure_ascii=False))

    out = "\n".join(lines) + "\n"
    with open(OUT_PATH, "w") as f:
        f.write(out)
    return OUT_PATH


if __name__ == "__main__":
    print("fixture escrita:", build())