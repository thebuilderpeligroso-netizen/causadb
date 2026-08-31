"""Builder de la fixture de sesión Cursor (Artículo IX: datos reales).

Extrae un fragmento REAL de ``~/.cursor/projects/<project>/agent-transcripts/
<session-uuid>/<session-uuid>.jsonl`` y lo guarda sanitizado en
``tests/fixtures/cursor_agent_transcript.jsonl``:

  - Reemplaza paths absolutos del home real por ``__FIXTURE_HOME__``.
  - Recorta textos >200 chars con ``[recortado-fixture]``.
  - Preserva la estructura JSONL original (una línea por mensaje).

Uso:
    .venv/bin/python tests/fixtures/_build_cursor_fixture.py
"""

import json
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "cursor_agent_transcript.jsonl")
DEFAULT_SESSION_DIR = os.path.expanduser(
    "~/.cursor/projects/empty-window/agent-transcripts/"
    "eabad476-ff5e-4fe6-ab82-79d7ad85ee09"
)


def _trim(s: str, limit: int) -> str:
    """Recorta un string largo preservando JSON válido."""
    if s is None:
        return s
    if len(s) <= limit:
        return s
    return s[:limit] + "[recortado-fixture]"


def _sanitize_paths(s: str) -> str:
    """Reemplaza paths absolutos del home real por __FIXTURE_HOME__."""
    home = os.path.expanduser("~")
    return s.replace(home, "__FIXTURE_HOME__")


def build(session_dir: str | None = None) -> str:
    session_dir = session_dir or os.environ.get(
        "CURSOR_SESSION_DIR", DEFAULT_SESSION_DIR
    )
    session_uuid = os.path.basename(session_dir)
    jsonl_path = os.path.join(session_dir, f"{session_uuid}.jsonl")

    if not os.path.exists(jsonl_path):
        raise FileNotFoundError(
            f"No se encontró {jsonl_path}. "
            "Pasá CURSOR_SESSION_DIR o el path como argumento."
        )

    with open(jsonl_path) as f:
        raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]

    lines: list[str] = []
    for raw_line in raw_lines:
        obj = json.loads(raw_line)
        msg = obj.get("message", {})
        content = msg.get("content", [])

        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    c["text"] = _sanitize_paths(_trim(c.get("text", ""), 200))
                elif c.get("type") == "tool_use":
                    inp = c.get("input", {})
                    for k in list(inp.keys()):
                        if isinstance(inp[k], str):
                            inp[k] = _sanitize_paths(_trim(inp[k], 200))
                elif c.get("type") == "tool_result":
                    res_content = c.get("content", "")
                    if isinstance(res_content, str):
                        c["content"] = _sanitize_paths(_trim(res_content, 200))

        lines.append(json.dumps(obj, ensure_ascii=False))

    out = "\n".join(lines) + "\n"
    with open(OUT_PATH, "w") as f:
        f.write(out)
    return OUT_PATH


if __name__ == "__main__":
    print("fixture escrita:", build())