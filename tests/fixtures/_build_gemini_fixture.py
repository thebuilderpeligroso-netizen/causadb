"""Builder de la fixture de sesión gemini-cli (Artículo IX: datos reales).

Extrae un fragmento REAL de ``~/.gemini/tmp/<proyecto>/chats/session-*.jsonl``
y lo guarda recortado en ``tests/fixtures/gemini_session_fragment.jsonl``:

  - línea metadata  ``{"kind":"main"}``                    (real, verbatim)
  - línea ``$set`` con array ``messages`` anidado           (recortada)
  - mensaje user real                                      (texto recortado)
  - línea ``$set`` ``lastUpdated``                          (real, verbatim)
  - mensaje gemini con thoughts (SIN toolCalls → emisión 1) (recortado)
  - línea ``$set`` ``lastUpdated``                          (real, verbatim)
  - re-emisión del mismo mensaje CON toolCalls (emisión 2)  (result recortado)

La re-emisión del mensaje con toolCalls es un detalle REAL del oplog de
gemini-cli: el mensaje aparece dos veces — la primera sin ``toolCalls``
(snapshot en-progreso) y la segunda con el resultado de la tool. El parser
debe deduplicar por message id quedándose con la última emisión.

Uso:
    .venv/bin/python tests/fixtures/_build_gemini_fixture.py
"""

import json
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "gemini_session_fragment.jsonl")
DEFAULT_SESSION = os.path.expanduser(
    "~/.gemini/tmp/cortex-agents/chats/session-2026-07-02T18-18-0d212d94.jsonl"
)


def _trim(s: str, limit: int) -> str:
    """Recorta un string largo preservando JSON válido (anti-datos-grandes)."""
    if s is None:
        return s
    if len(s) <= limit:
        return s
    return s[:limit] + "…[recortado-fixture]"


def _trim_text_parts(content) -> None:
    """Recorta en-place los strings ``text`` de una lista de partes."""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = _trim(part["text"], 220)


def build(session_path: str | None = None) -> str:
    session_path = session_path or os.environ.get("GEMINI_SESSION_PATH", DEFAULT_SESSION)
    with open(session_path) as f:
        raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]

    # --- línea 1: metadata (verbatim) ---
    meta = raw_lines[0]
    assert '"kind":"main"' in meta, "línea 1 debe ser metadata"

    # --- línea 2: $set con messages (recortada) ---
    set_messages = json.loads(raw_lines[1])
    assert "$set" in set_messages and "messages" in set_messages["$set"]
    for m in set_messages["$set"]["messages"]:
        _trim_text_parts(m.get("content"))

    # --- líneas 3-6: user msg, $set lastUpdated, gemini thoughts, $set ---
    user_msg = json.loads(raw_lines[2])
    assert user_msg.get("type") == "user"
    _trim_text_parts(user_msg.get("content"))
    set_lu1 = raw_lines[3]
    assert "$set" in set_lu1

    gemini_msg = json.loads(raw_lines[4])
    assert gemini_msg.get("type") == "gemini" and "toolCalls" not in gemini_msg
    for th in gemini_msg.get("thoughts") or []:
        th["description"] = _trim(th.get("description", ""), 300)
    set_lu2 = raw_lines[5]
    assert "$set" in set_lu2

    # --- línea 7: re-emisión CON toolCalls (result recortado) ---
    gemini_tool = json.loads(raw_lines[6])
    assert gemini_tool.get("type") == "gemini" and "toolCalls" in gemini_tool
    assert gemini_tool["id"] == gemini_msg["id"], "debe ser re-emisión del mismo mensaje"
    for th in gemini_tool.get("thoughts") or []:
        th["description"] = _trim(th.get("description", ""), 300)
    for tc in gemini_tool.get("toolCalls") or []:
        res = tc.get("result")
        if isinstance(res, list):
            for fr in res:
                if isinstance(fr, dict) and isinstance(fr.get("functionResponse"), dict):
                    resp = fr["functionResponse"].get("response")
                    if isinstance(resp, dict) and isinstance(resp.get("output"), str):
                        resp["output"] = _trim(resp["output"], 180)

    lines = [
        meta,
        json.dumps(set_messages, ensure_ascii=False),
        json.dumps(user_msg, ensure_ascii=False),
        set_lu1,
        json.dumps(gemini_msg, ensure_ascii=False),
        set_lu2,
        json.dumps(gemini_tool, ensure_ascii=False),
    ]
    out = "\n".join(lines) + "\n"
    with open(OUT_PATH, "w") as f:
        f.write(out)
    return OUT_PATH


if __name__ == "__main__":
    print("fixture escrita:", build())
