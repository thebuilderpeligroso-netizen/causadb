"""Sedimentación de BIT-entries al CAUSADB_CHRONICLE.md (template curado).

Reemplaza el edit manual del agente sobre el Chronicle: ``render_entry``
genera la entrada con el template curado (formato nuevo ``**Autor:**`` /
``**Naturaleza:**``, ver BIT-CHR.105) y ``append_entry`` la apendea con
idempotencia, concurrencia (flock) y durabilidad (fsync).

El ledger NO se toca acá — el ledger es la columna vertebral (Art. I). La
alineación ledger ↔ .md la garantiza el ``bit_id`` compartido + el
``event_id`` opcional citado en ``**Referencias:**`` (formato que
``_PROSE_EVENT_ID_RE`` captura en ``_chronicle_index.py``).

FAIL-CLOSED (Art. VIII): sin chronicle resoluble o con campos requeridos
faltantes → ``ValueError``. El caller (CLI/MCP) decide cómo exponerlo.
"""
import fcntl
import os
import re
from typing import List, Optional


def render_entry(
    bit_id: str,
    title: str,
    date: str,
    author: str,
    nature: str,
    summary: Optional[str] = None,
    files: Optional[List[str]] = None,
    body: str = "",
    event_id: Optional[str] = None,
) -> str:
    """Renderiza un BIT-entry con el template curado.

    Formato (alineado con las entradas nuevas del Chronicle real, BIT-CHR.105):

        ## BIT-{id} — {title}

        **Fecha:** {date}
        **Autor:** {author}
        **Naturaleza:** {nature}

        {body}

        **Referencias:** event_id: {event_id}   (solo si event_id)

        ---

    ``summary``/``files`` se aceptan por compatibilidad de firma pero NO se
    renderizan: el template curado los cubre en el body (decisión empírica
    sobre el formato real del .md — las entradas nuevas no llevan
    ``**Resumen:**``/``**Archivos tocados:**``). Los campos que render_entry
    genera (Fecha/Autor/Naturaleza) son parseables por ``_parse_entry_body``
    actualizado.
    """
    parts = [
        f"## {bit_id} — {title}",
        "",
        f"**Fecha:** {date}",
        f"**Autor:** {author}",
        f"**Naturaleza:** {nature}",
        "",
        body,
    ]
    if event_id:
        parts += ["", f"**Referencias:** event_id: {event_id}"]
    parts += ["", "---"]
    return "\n".join(parts) + "\n"


# Idempotencia: el BIT existe si la línea `## BIT-<id>` aparece seguida de
# separador (espacio/em-dash/en-dash/hyphen) o fin de línea. `BIT-1` NO
# matchea `BIT-10`/`BIT-105` (prefijo exacto, anti-falso-positivo).
# NOTA: `bit_id` ya incluye el prefijo "BIT-" (ej. "BIT-CHR.999") — el
# template NO agrega otro "BIT-" (desvío del plan: el template literal
# `^##\s+BIT-{bit_id}` duplicaba el prefijo → `BIT-BIT-...`).
def _bit_exists(content: str, bit_id: str) -> bool:
    """True si el bit_id EXACTO ya tiene una entrada en el .md."""
    return bool(
        re.search(
            rf"^##\s+{re.escape(bit_id)}([\s—–-]|$)",
            content,
            re.MULTILINE,
        )
    )


def append_entry(
    ledger_path: str,
    chronicle_path: Optional[str] = None,
    *,
    bit_id: str,
    title: str,
    date: str,
    author: str,
    nature: str,
    summary: Optional[str] = None,
    files: Optional[List[str]] = None,
    body: str = "",
    event_id: Optional[str] = None,
) -> dict:
    """Apendea un BIT-entry al CAUSADB_CHRONICLE.md (idempotente, FAIL-CLOSED).

    Resolución del path:
      - ``chronicle_path`` explícito → autoritativo: se usa tal cual y se
        CREA si no existe (desvío documentado: ``resolve_chronicle_path``
        devuelve None para archivos explícitos inexistentes — FAIL-CLOSED
        pensado para auto-discovery, no para un destino explícito de
        sedimentación; los tests y la demo exigen crear el archivo).
      - ``chronicle_path=None`` → auto-discovery vía
        ``resolve_chronicle_path`` (config.json → dirname(ledger) → walk-up
        ``<ancestor>/causadb/CAUSADB_CHRONICLE.md``). Si nada existe →
        ``ValueError`` FAIL-CLOSED.

    Concurrencia: ``fcntl.flock`` sobre ``<chronicle_path>.lock`` alrededor
    del check+append (patrón del ledger, ``_ledger_writer.py:234-289``).

    Idempotencia: si el ``bit_id`` exacto ya existe → retorna
    ``{"status": "already_exists", ...}`` sin duplicar.

    Durabilidad: append con ``open(path, "a")`` + ``flush`` + ``os.fsync``.

    Post-append: ``rebuild_index`` best-effort (try/except — no crashea si
    el ledger no existe o el índice falla; el .md ya está escrito).

    Returns:
        ``{"status": "appended"|"already_exists", "bit_id", "chronicle_path"}``

    Raises:
        ValueError: chronicle no resuelto (FAIL-CLOSED) o campos requeridos
        faltantes (bit_id, title, date, author, body).
    """
    # -- Resolución del path -------------------------------------------------
    if chronicle_path is not None:
        path = chronicle_path
    else:
        from causadb._chronicle_index import resolve_chronicle_path
        path = resolve_chronicle_path(ledger_path, None)
        if path is None:
            raise ValueError(
                f"CAUSADB_CHRONICLE.md no encontrado para ledger {ledger_path} "
                "(FAIL-CLOSED: no se puede sedimentar sin el chronicle; "
                "pasarlo explícito con --chronicle-path)"
            )

    # -- Validación de campos requeridos (FAIL-CLOSED) ----------------------
    missing = [
        name
        for name, value in (
            ("bit_id", bit_id),
            ("title", title),
            ("date", date),
            ("author", author),
            ("body", body),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing required fields: {missing} "
            "(required: bit_id, title, date, author, body)"
        )

    # -- Check + append bajo flock (concurrencia) ---------------------------
    lock_path = path + ".lock"
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            content = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            if _bit_exists(content, bit_id):
                return {
                    "status": "already_exists",
                    "bit_id": bit_id,
                    "chronicle_path": path,
                }

            rendered = render_entry(
                bit_id, title, date, author, nature,
                summary=summary, files=files, body=body, event_id=event_id,
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(rendered)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # -- Rebuild del índice best-effort (nunca crashea el append) -----------
    try:
        from causadb._chronicle_index import rebuild_index
        rebuild_index(ledger_path, path)
    except Exception:
        pass

    return {
        "status": "appended",
        "bit_id": bit_id,
        "chronicle_path": path,
    }