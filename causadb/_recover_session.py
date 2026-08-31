"""Recovery de sesiones desde la fuente cruda — Fase 13 (ver Chronicle; ver docs/design_index.md).

Capa 4 del círculo de contexto. Dada una ``session_id`` (o un keyword de
búsqueda), recupera el detalle completo de la sesión desde la FUENTE
CRUDA de cada herramienta — NO desde el harvest normalizado, que es lossy
por diseño (opencode descarta los parts ``text``/``file``/``patch`` donde
viven prompts y respuestas; grok no mapea tool calls). Composición DRY:
se reusa ``build_storyboard`` (Fase 12).

Cada extractor recibe la instancia de la puntita (ya configurada con el
path del store) y devuelve raw events canónicos (misma forma que el
harvest) filtrando a la sesión pedida. Estrategia por herramienta:

  - **opencode** (extractor bespoke): lee la sesión completa con TODOS
    sus parts por ``rowid`` — ``text`` de usuario → REASONING_STEP
    ``user_prompt``; ``text`` de assistant → LLM_INVOKED; ``reasoning`` →
    REASONING_STEP; ``tool`` → TOOL_CALLED; ``file``/``patch`` →
    FILE_MODIFIED. Los parts que la puntita descarta se restauran acá.
  - **gemini / claude / grok** (reuso de ``_harvest_file``): se localiza
    el archivo de la sesión en el oplog/updates.jsonl y se parsea
    completo (su parse ya produce detalle íntegro por sesión).
  - **hermes / openjarvis** (reuso de ``harvest``): los raws ya llevan
    detalle completo y van taggeados con ``__harvest_session_id``; se
    filtra por sesión.

``recover_session`` es el orquestador: auto-detecta la fuente (todas las
puntitas ``detect()``) → si >1 herramienta matchea la sesión →
``AmbiguousSessionError`` (requiere ``--tool`` explícito); si 0 →
``SessionNotFoundError``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from causadb._agent_transcript import infer_step_type
from causadb._harvest_source_opencode import _ms_to_iso, _synthesize_subject
from causadb._storyboard import build_storyboard


class SessionNotFoundError(Exception):
    """La sesión pedida no existe en ninguna fuente detectada."""


class AmbiguousSessionError(Exception):
    """La sesión existe en más de una herramienta → requiere ``--tool``."""


def _source_classes() -> dict[str, type]:
    """Puntitas por tool (import tardío: las fuentes importan HarvestSource)."""
    from causadb._harvest_source_opencode import OpenCodeHarvestSource
    from causadb._harvest_source_gemini import GeminiHarvestSource
    from causadb._harvest_source_claude import ClaudeHarvestSource
    from causadb._harvest_source_grok import GrokHarvestSource
    from causadb._harvest_source_hermes import HermesHarvestSource
    from causadb._harvest_source_openjarvis import OpenJarvisHarvestSource
    from causadb._harvest_source_codex import CodexHarvestSource
    from causadb._harvest_source_cursor import CursorHarvestSource
    from causadb._harvest_source_windsurf import WindsurfHarvestSource
    from causadb._harvest_source_n8n import N8nHarvestSource
    from causadb._harvest_source_freqtrade import FreqtradeHarvestSource

    return {
        "opencode": OpenCodeHarvestSource,
        "gemini": GeminiHarvestSource,
        "claude": ClaudeHarvestSource,
        "grok": GrokHarvestSource,
        "hermes": HermesHarvestSource,
        "openjarvis": OpenJarvisHarvestSource,
        "codex": CodexHarvestSource,
        "cursor": CursorHarvestSource,
        "windsurf": WindsurfHarvestSource,
        "n8n": lambda lp: N8nHarvestSource(lp),
        "freqtrade": lambda lp: FreqtradeHarvestSource(lp),
    }


# ---------------------------------------------------------------- extractors

# C.4 — Mapeo de locator_kind → tool name. Un `conversation_ref` es confiable
# solo si su locator_kind/resolver coincide con una fuente de agente
# recuperable (con extractor). Si no, el recover degrada al mecanismo actual.
_LOCATOR_TOOL_MAP = {
    "opencode": "opencode",
    "gemini": "gemini",
    "claude": "claude",
    "grok": "grok",
    "hermes": "hermes",
    "openjarvis": "openjarvis",
    "codex": "codex",
    "cursor": "cursor",
    "windsurf": "windsurf",
}

_RELIABLE_LOCATOR_KINDS = frozenset({"sqlite", "jsonl", "updates.jsonl", "file"})

_INVALID_LOCATOR_KINDS = frozenset({
    "", "none", "unknown", "memory", "inferred", "harvest", "oplog",
})


def _resolve_provider(conversation_ref: dict) -> Optional[str]:
    """Resuelve el tool name desde un ``conversation_ref`` (contrato C.2).

    Devuelve el tool name solo si el ref es confiable (provider conocido con
    extractor + locator_kind válido). Devuelve ``None`` en cualquier caso
    dudoso → el recover DEGRADA al mecanismo actual (auto-detección por
    recorrido de fuentes) y lo reporta. Nunca lanza.
    """
    if not isinstance(conversation_ref, dict):
        return None

    provider = conversation_ref.get("provider") or conversation_ref.get("resolver")
    if provider not in _LOCATOR_TOOL_MAP:
        return None

    kind = (conversation_ref.get("locator_kind") or "").lower()
    if kind in _INVALID_LOCATOR_KINDS:
        return None
    if conversation_ref.get("locator_kind") is None and kind == "":
        # sin locator_kind declarado → no confiable
        return None

    # provider válido + extractor presente
    if _LOCATOR_TOOL_MAP[provider] not in _EXTRACTORS:
        return None

    return _LOCATOR_TOOL_MAP[provider]


def _extract_opencode(source, session_id: str) -> Optional[list[dict]]:
    """Extractor bespoke opencode: TODOS los parts de la sesión (text/file/
    patch incluidos) → raw events canónicos."""
    con = sqlite3.connect(f"file:{source.db_path}?mode=ro", uri=True)
    try:
        sess = con.execute(
            "SELECT id FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if sess is None:
            return None
        raws: list[dict] = []
        msg_rows = con.execute(
            "SELECT id, time_created, data, rowid FROM message "
            "WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
        for mid, mtime, mdata, mrowid in msg_rows:
            try:
                m = json.loads(mdata)
            except (json.JSONDecodeError, TypeError):
                continue
            role = m.get("role", "")
            part_rows = con.execute(
                "SELECT time_created, data, rowid FROM part "
                "WHERE message_id = ? ORDER BY rowid",
                (mid,),
            ).fetchall()
            content_parts: list[dict] = []
            assistant_active = False
            for ptime, pdata, prowid in part_rows:
                try:
                    d = json.loads(pdata)
                except (json.JSONDecodeError, TypeError):
                    continue
                ptype = d.get("type")
                ts_ms = (d.get("time") or {}).get("start") or ptime or mtime
                ts = _ms_to_iso(int(ts_ms or 0))
                if role == "user" and ptype == "text":
                    raws.append({
                        "type": "REASONING_STEP",
                        "timestamp": ts,
                        "step_type": "user_prompt",
                        "description": d.get("text", "") or "",
                        "agent": "opencode",
                        "__harvest_session_id": session_id,
                    })
                    continue
                if role != "assistant":
                    continue
                if ptype == "reasoning":
                    assistant_active = True
                    text = d.get("text") or ""
                    subject = _synthesize_subject(text)
                    raws.append({
                        "type": "REASONING_STEP",
                        "timestamp": ts,
                        "step_type": infer_step_type(subject),
                        "step_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "subject": subject,
                        "description": text,
                        "agent": "opencode",
                        "__harvest_session_id": session_id,
                    })
                elif ptype == "tool":
                    assistant_active = True
                    state = d.get("state") or {}
                    raws.append({
                        "type": "TOOL_CALLED",
                        "timestamp": ts,
                        "tool_name": d.get("tool") or "unknown_tool",
                        "arguments": state.get("input") or {},
                        "result": state.get("output") or "",
                        "tool_call_id": d.get("callID"),
                        "agent": "opencode",
                        "__harvest_session_id": session_id,
                    })
                elif ptype in ("file", "patch"):
                    assistant_active = True
                    # Schema real: patch → {"files": [...]}; file → {"filename"}.
                    paths = d.get("files") or []
                    if not paths and ptype == "file" and d.get("filename"):
                        paths = [d.get("filename")]
                    for path in paths:
                        if path:
                            raws.append({
                                "type": "FILE_MODIFIED",
                                "timestamp": ts,
                                "path": path,
                                "agent": "opencode",
                                "__harvest_session_id": session_id,
                            })
                if ptype == "text":
                    assistant_active = True
                    content_parts.append(d.get("text", "") or "")
            if assistant_active:
                tokens = m.get("tokens") or {}
                model = (m.get("model") or {}).get("modelID")
                raws.append({
                    "type": "LLM_INVOKED",
                    "timestamp": _ms_to_iso(int(mtime)),
                    "model": model or "opencode",
                    "prompt": "",
                    "response_tokens": int(tokens.get("output") or 0),
                    "response_content": "\n".join(content_parts),
                    "agent": "opencode",
                    "__harvest_session_id": session_id,
                })
        return raws or None
    finally:
        con.close()


def _find_session_file(source, session_id: str, key_fn) -> Optional[str]:
    """Localiza el archivo crudo de la sesión vía el iterador de la puntita."""
    # GAP-01: gemini ahora itera por store (chats_dirs); el relpath del
    # cursor es basename en single-store. Las demás puntitas (claude/grok/
    # codex/cursor) conservan el protocolo (relpath, fpath) sin argumentos.
    if hasattr(source, "chats_dirs"):
        for chats_dir in source.chats_dirs:
            for fpath in source._iter_sessions_by_mtime(chats_dir):
                relpath = os.path.basename(fpath)
                if key_fn(relpath, fpath) == session_id:
                    return fpath
        return None
    for relpath, fpath in source._iter_sessions_by_mtime():
        if key_fn(relpath, fpath) == session_id:
            return fpath
    return None


def _extract_file_based(source, session_id: str, key_fn) -> Optional[list[dict]]:
    """Extractor para gemini/claude/grok: reusa ``_harvest_file`` de la
    puntita (parse completo del archivo) y filtra por sesión."""
    fpath = _find_session_file(source, session_id, key_fn)
    if fpath is None:
        return None
    raw_events = source._harvest_file(session_id, fpath, {})
    return raw_events or None


def _extract_gemini(source, session_id: str) -> Optional[list[dict]]:
    # session_id gemini = basename ("session-xxx.jsonl").
    return _extract_file_based(
        source, session_id, lambda rel, _fp: os.path.basename(rel)
    )


def _extract_claude(source, session_id: str) -> Optional[list[dict]]:
    # session_id claude = basename del jsonl (con extensión).
    return _extract_file_based(
        source, session_id, lambda rel, _fp: os.path.basename(rel)
    )


def _extract_grok(source, session_id: str) -> Optional[list[dict]]:
    # session_id grok = basename del dir que contiene updates.jsonl.
    return _extract_file_based(
        source,
        session_id,
        lambda _rel, fp: os.path.basename(os.path.dirname(fp)),
    )


def _extract_sql_session(source, session_id: str) -> Optional[list[dict]]:
    """Extractor para hermes/openjarvis: reusa ``harvest`` (detalle íntegro)
    y filtra por ``__harvest_session_id``."""
    # Hermes can constrain its SQLite query by native session ID. Keep the
    # harvest fallback for older/custom sources without that capability.
    if hasattr(source, "harvest_session"):
        raw_events = source.harvest_session(session_id)
    else:
        raw_events = source.harvest({})
    matched = [
        r for r in raw_events
        if r.get("__harvest_session_id") == session_id
    ]
    return matched or None


# ------------------------------------------------------------- orquestación

_EXTRACTORS = {
    "opencode": _extract_opencode,
    "gemini": _extract_gemini,
    "claude": _extract_claude,
    "grok": _extract_grok,
    "hermes": _extract_sql_session,
    "openjarvis": _extract_sql_session,
    "codex": _extract_sql_session,
    "cursor": _extract_sql_session,
    "windsurf": _extract_sql_session,
}


def recover_session(
    ledger_path: str,
    session_id: str,
    tool: Optional[str] = None,
    conversation_ref: Optional[dict] = None,
) -> tuple[str, dict]:
    """Recupera el storyboard de una sesión desde la fuente cruda.

    Args:
        ledger_path: Ruta absoluta al ledger (para instanciar las puntitas).
        session_id: Id de la sesión a recuperar (ver docstring por tool).
        tool: Herramienta explícita (opencode|gemini|claude|grok|hermes|
            openjarvis). Si se omite, se auto-detecta.
        conversation_ref: Ref del contrato C.2 (si viene de revive/ledger).
            Si es confiable, resuelve el provider SIN recorrer fuentes
            (C.4.1); si no lo es, DEGRADA al mecanismo actual y el detalle
            queda en el storyboard (note).

    Returns:
        ``(tool, storyboard)`` con el detalle completo de la sesión.

    Raises:
        SessionNotFoundError: la sesión no existe en ninguna fuente.
        AmbiguousSessionError: la sesión existe en >1 herramienta.
    """
    if tool:
        return _recover_one(ledger_path, session_id, tool)

    # C.4.1 — Lookup por locator: si el ref es confiable, no recorremos
    # fuentes ni hacemos harvest completo. Fallback explícito al recorrido
    # (degradación controlada).
    resolved_tool = _resolve_provider(conversation_ref) if conversation_ref else None
    if resolved_tool:
        tool_out, storyboard = _recover_one(ledger_path, session_id, resolved_tool)
        storyboard = dict(storyboard)
        storyboard.setdefault("note", "")
        note = storyboard["note"]
        note = f"{note} (via conversation_ref, provider={resolved_tool})" if note else (
            f"Resolved via conversation_ref (provider={resolved_tool})."
        )
        storyboard["note"] = note
        return resolved_tool, storyboard

    # Degradación: si conversation_ref venía pero no era confiable, caemos
    # al recorrido clásico de fuentes (auto-detección). Explícito, no excepcional.
    found: list[tuple[str, list[dict]]] = []
    for name, cls in _source_classes().items():
        if name not in _EXTRACTORS:
            continue  # sin extractor → no es fuente de agente recuperable
        # Algunas fuentes son lambdas (n8n, freqtrade) que esperan
        # argumento posicional; otras son clases que aceptan keyword.
        try:
            source = cls(ledger_path=ledger_path)
        except TypeError:
            source = cls(ledger_path)
        if not source.detect():
            continue
        try:
            raws = _EXTRACTORS[name](source, session_id)
        except (sqlite3.Error, OSError, ValueError):
            continue  # store corrupto/ausente: no romper la auto-detección
        if raws:
            found.append((name, raws))

    if not found:
        raise SessionNotFoundError(
            f"Session '{session_id}' not found in any detected source."
        )
    if len(found) > 1:
        tools = ", ".join(sorted(t for t, _ in found))
        raise AmbiguousSessionError(
            f"Session '{session_id}' exists in multiple tools ({tools}). "
            "Pass --tool explicitly."
        )

    name, raws = found[0]
    return _recover_one(ledger_path, session_id, name)


def _recover_one(ledger_path: str, session_id: str, tool: str) -> tuple[str, dict]:
    cls = _source_classes().get(tool)
    if cls is None:
        raise ValueError(
            f"Unknown tool '{tool}'. Expected one of: "
            + ", ".join(sorted(_source_classes()))
        )
    try:
        source = cls(ledger_path=ledger_path)
    except TypeError:
        source = cls(ledger_path)
    if not source.detect():
        raise SessionNotFoundError(
            f"Source '{tool}' not present (store not detected)."
        )
    try:
        raws = _EXTRACTORS[tool](source, session_id)
    except (sqlite3.Error, OSError, ValueError):
        raise SessionNotFoundError(
            f"Session '{session_id}' not found in source '{tool}'."
        )
    if not raws:
        raise SessionNotFoundError(
            f"Session '{session_id}' not found in source '{tool}'."
        )
    storyboard = build_storyboard(raws, tool=tool)
    if storyboard is None:
        # Sin LLM_INVOKED no hay turnos que detallar: reportar la sesión
        # igualmente con los raws crudos (decisión, no error).
        storyboard = {
            "tool": tool,
            "session_id": raws[0].get("__harvest_session_id", session_id),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "turn_count": 0,
            "raw_events": len(raws),
            "note": "No LLM_INVOKED in raw source for this session.",
        }
    return tool, storyboard


def search_stories(
    ledger_path: str,
    keyword: str,
    tool: Optional[str] = None,
) -> list[dict]:
    """Busca un keyword en los storyboards persistidos (Fase 12).

    El ``session_id`` se lee del CONTENIDO del storyboard (no del nombre de
    archivo sanitizado, que puede perder información). Retorna la lista de
    storyboards que matchean, cada uno con ``file`` (relativo al dir base).
    """
    from causadb._config import CausaDBConfig

    base = CausaDBConfig(
        ledger_path=ledger_path, redaction_enabled=False, telemetry_enabled=False
    ).storyboard_path
    if not base or not os.path.isdir(base):
        return []

    needle = keyword.lower()
    matches: list[dict] = []
    tools = [tool] if tool else os.listdir(base)
    for t in tools:
        tdir = os.path.join(base, t)
        if not os.path.isdir(tdir):
            continue
        for fname in sorted(os.listdir(tdir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(tdir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    sb = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(sb, dict):
                continue
            haystack = " ".join(
                str(sb.get(k, "")) for k in (
                    "session_id", "tool", "note",
                )
            )
            for turn in sb.get("turns", []) or []:
                if not isinstance(turn, dict):
                    continue
                haystack += " " + " ".join(
                    str(turn.get(k, "")) for k in ("prompt", "assistant_response")
                )
                haystack += " " + " ".join(
                    str(r) for r in turn.get("reasoning", []) or []
                )
            for tc in sb.get("tool_calls", []) or []:
                if isinstance(tc, dict):
                    haystack += " " + str(tc.get("tool_name", ""))
                    haystack += " " + str(tc.get("input", ""))
                    haystack += " " + str(tc.get("result", ""))
            for d in sb.get("decisions", []) or []:
                if isinstance(d, dict):
                    haystack += " " + str(d.get("reasoning", ""))
            for e in sb.get("errors", []) or []:
                if isinstance(e, dict):
                    haystack += " " + str(e.get("error", ""))
            haystack += " " + " ".join(str(f) for f in sb.get("files_touched", []) or [])
            if needle in haystack.lower():
                matches.append({
                    "tool": t,
                    "session_id": sb.get("session_id", fname[:-5]),
                    "file": os.path.join(t, fname),
                    "turn_count": sb.get("turn_count", 0),
                })
    return matches
