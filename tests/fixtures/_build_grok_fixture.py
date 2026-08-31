"""Genera el fixture de Grok Build: ``tests/fixtures/grok_fixture/``.

Copia VERBATIM (bytes) el ``updates.jsonl`` real de la ÚNICA sesión de
``~/.grok/sessions/`` (Artículo IX — datos reales, no mocks; auditoría del
operador 2026-08-02):

  - ``%2Fhome%2Fjuliussb/019f492c-79d3-75b3-9651-95142b28c3c6/updates.jsonl``
    (1.619 bytes, 3 líneas)

La ruta relativa (relpath) se preserva tal cual (patrón de cursor del
harvest: ``{"files": {relpath: {mtime, offset}}}``).

Fuente primaria (DECISIÓN del operador, 2026-08-02): ``updates.jsonl`` es
"the authoritative conversation log" del user guide oficial
(17-sessions.md) — trae timestamp (epoch SEGUNDOS) + modelId.
``chat_history.jsonl`` se descarta: no tiene timestamp ni id, es el espejo
de input del turno (5 líneas: 1 system + 4 user, sin assistant). La
desviación del plan ("chat_history primaria") se documenta en el docstring
de ``_harvest_source_grok.py``.

Consecuencia real verificada: la ÚNICA sesión es un turno FALLIDO (auth
403 — ``retry_state`` type=failed + ``turn_completed`` stop_reason=error)
→ el harvest real de la fixture produce **0 eventos**. Honesto (Artículo
IX: no inventar datos de happy-path que no existen localmente). El
happy-path se cubre con unit test SINTÉTICO explícitamente no-fixture.

Re-ejecutar para regenerar:
    python tests/fixtures/_build_grok_fixture.py
"""

import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "grok_fixture")

# El updates.jsonl real de la única sesión (2026-08-02), relpath preservado
# (patrón del cursor por archivo del harvest).
SESSION_RELPATH = (
    "%2Fhome%2Fjuliussb/019f492c-79d3-75b3-9651-95142b28c3c6/updates.jsonl"
)

EXPECTED_LINES = 3
EXPECTED_SESSION_UPDATES = [
    "retry_state",       # type=failed, error_type=api (auth 403) → SALTAR
    "user_message_chunk",  # content.text="." + _meta.modelId="grok-4.5"
    "turn_completed",    # stop_reason=error (turno fallido) → SALTAR
]
EXPECTED_MODEL_ID = "grok-4.5"


def _source_sessions_dir() -> str:
    env_path = os.environ.get("CAUSADB_GROK_SESSIONS_DIR")
    if env_path:
        return env_path
    return os.path.expanduser("~/.grok/sessions")


def _verify_session(src: str) -> None:
    """Verifica COUNTs y estructura del updates.jsonl real ANTES de
    reemplazar el fixture (Artículo IX — la fixture debe reflejar el store
    real)."""
    with open(src, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    if len(lines) != EXPECTED_LINES:
        raise AssertionError(
            f"El updates.jsonl real cambió: {len(lines)} líneas (esperaba "
            f"{EXPECTED_LINES}). Revisar antes de regenerar la fixture."
        )
    session_updates = []
    timestamps = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"Línea JSON inválida en el updates.jsonl real: {e}"
            ) from e
        update = (obj.get("params") or {}).get("update") or {}
        session_updates.append(update.get("sessionUpdate"))
        # El timestamp top-level debe ser epoch SEGUNDOS (int/float real).
        ts = obj.get("timestamp")
        timestamps.append(ts)
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise AssertionError(
                f"timestamp epoch segundos esperado, obtuvo {ts!r}"
            )
    if session_updates != EXPECTED_SESSION_UPDATES:
        raise AssertionError(
            f"La secuencia de sessionUpdate real cambió:\n  {session_updates}\n"
            f"  (esperaba {EXPECTED_SESSION_UPDATES}). Revisar antes de regenerar."
        )
    # turn_completed: stop_reason == "error" → el harvest real da 0 eventos.
    turn = json.loads(lines[2])
    turn_update = (turn.get("params") or {}).get("update") or {}
    if turn_update.get("stop_reason") != "error":
        raise AssertionError(
            "El turn_completed real debe traer stop_reason='error' (turno "
            "fallido) para que el harvest real dé 0 eventos — cambió."
        )
    # user_message_chunk: _meta.modelId == "grok-4.5" (model de la sesión).
    chunk = json.loads(lines[1])
    chunk_update = (chunk.get("params") or {}).get("update") or {}
    if chunk_update.get("sessionUpdate") != "user_message_chunk":
        raise AssertionError(
            "La línea 2 real debe ser user_message_chunk — cambió."
        )
    chunk_meta = chunk_update.get("_meta") or {}
    if chunk_meta.get("modelId") != EXPECTED_MODEL_ID:
        raise AssertionError(
            f"El _meta.modelId real del user_message_chunk debe ser "
            f"'{EXPECTED_MODEL_ID}' — cambió (obtuvo {chunk_meta.get('modelId')!r})."
        )


def _build() -> None:
    src = os.path.join(_source_sessions_dir(), SESSION_RELPATH)
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"Store real de Grok Build no encontrado: {src}. "
            "No se puede regenerar la fixture (Artículo IX: datos reales)."
        )

    _verify_session(src)

    dest = os.path.join(OUT_DIR, SESSION_RELPATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # Copia verbatim (bytes) — el updates.jsonl íntegro, sin recortar.
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
