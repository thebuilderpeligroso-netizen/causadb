"""Genera el fixture de Claude Code: ``tests/fixtures/claude_fixture/``.

Copia VERBATIM UNA sesión real de ``~/.claude/projects/`` (Artículo IX —
datos reales, no mocks), la más chica de las 2 existentes (verificadas el
2026-08-02):

  - ``-home-juliussb--local-share-open-design--od-projects-365fastfood-landing/
     11919c6c-9995-4d1b-b1ea-144fc3c64e8a.jsonl`` (64.228 bytes, 9 líneas)

La ruta relativa (relpath) se preserva tal cual (patrón de cursor del
harvest: ``{"files": {relpath: {mtime, offset, last_message_id}}}``).

Consecuencia real verificada (2026-08-02): las 2 sesiones reales de
``~/.claude/projects/`` tienen el único assistant con ``isApiErrorMessage:
true`` + ``error: "authentication_failed"`` (auth fallida) → el harvest real
de la fixture produce **0 eventos** (todo salteable). La fixture ejercita:
parse real del JSONL, salteo de ``queue-operation``/``attachment``/
``last-prompt``/``isApiErrorMessage``, y el user message → ``content``
parseado (sin evento). Esto es lo honesto (Artículo IX: no inventar datos
de happy-path que no existen localmente).

NO se recortan líneas ni contenido (el fixture es la sesión íntegra, a
diferencia de las fixtures gemini/hermes que recortan campos largos; la
sesión real es de solo 64KB, tamaño aceptable igual que
``openjarvis_fixture.db``). El builder verifica COUNTs (9 líneas) y la
estructura top-level antes de reemplazar el fixture.

Re-ejecutar para regenerar:
    python tests/fixtures/_build_claude_fixture.py
"""

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "claude_fixture")

# La sesión real más chica de ~/.claude/projects (2026-08-02), relpath
# preservado (patrón del cursor por archivo del harvest).
SESSION_RELPATH = (
    "-home-juliussb--local-share-open-design--od-projects-365fastfood-landing/"
    "11919c6c-9995-4d1b-b1ea-144fc3c64e8a.jsonl"
)

EXPECTED_LINES = 9

# Tipos top-level esperados en el orden real de la sesión (verificado).
EXPECTED_TYPES = [
    "queue-operation",  # enqueue
    "queue-operation",  # dequeue
    "attachment",       # hook_success SessionStart:startup
    "user",             # único mensaje user real (no genera evento)
    "attachment",       # deferred_tools_delta
    "attachment",       # agent_listing_delta
    "attachment",       # skill_listing
    "assistant",        # isApiErrorMessage=true (auth fallida) → SALTAR
    "last-prompt",      # sin timestamp top-level → SALTAR
]


def _source_projects_dir() -> str:
    env_path = os.environ.get("CAUSADB_CLAUDE_PROJECTS_DIR")
    if env_path:
        return env_path
    return os.path.expanduser("~/.claude/projects")


def _verify_session(src: str) -> None:
    """Verifica COUNTs y estructura de la sesión real ANTES de reemplazar el
    fixture (Artículo IX — la fixture debe reflejar el store real)."""
    with open(src, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    if len(lines) != EXPECTED_LINES:
        raise AssertionError(
            f"La sesión real cambió: {len(lines)} líneas (esperaba "
            f"{EXPECTED_LINES}). Revisar antes de regenerar la fixture."
        )
    types = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"Línea JSON inválida en la sesión real: {e}"
            ) from e
        types.append(obj.get("type"))
    if types != EXPECTED_TYPES:
        raise AssertionError(
            f"La estructura top-level de la sesión real cambió:\n  {types}\n"
            f"  (esperaba {EXPECTED_TYPES}). Revisar antes de regenerar."
        )
    assistant = json.loads(lines[7])
    if not (assistant.get("isApiErrorMessage") and assistant.get("error")):
        raise AssertionError(
            "El assistant real de la sesión debe ser isApiErrorMessage=true "
            "(auth fallida) para que el harvest real dé 0 eventos — cambió."
        )


def _build() -> None:
    src = os.path.join(_source_projects_dir(), SESSION_RELPATH)
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"Store real de Claude Code no encontrado: {src}. "
            "No se puede regenerar la fixture (Artículo IX: datos reales)."
        )

    _verify_session(src)

    dest = os.path.join(OUT_DIR, SESSION_RELPATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # Copia verbatim (bytes) — la sesión íntegra, sin recortar líneas.
    shutil.copyfile(src, dest)

    # Verificación de la copia ANTES de darla por buena: byte-idéntica y
    # con el mismo conteo de líneas.
    with open(src, "rb") as f:
        src_bytes = f.read()
    with open(dest, "rb") as f:
        dest_bytes = f.read()
    if src_bytes != dest_bytes:
        os.remove(dest)
        raise AssertionError("La copia del fixture no es byte-idéntica al store real")
    if dest_bytes.count(b"\n") != EXPECTED_LINES:
        os.remove(dest)
        raise AssertionError(
            f"El fixture copiado tiene {dest_bytes.count(b'\n')} líneas "
            f"(esperaba {EXPECTED_LINES})"
        )

    print(
        f"fixture OK: {dest} ({len(dest_bytes)} bytes, "
        f"{dest_bytes.count(b'\n')} líneas, verbatim)"
    )


if __name__ == "__main__":
    _build()
