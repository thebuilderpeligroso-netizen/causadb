"""Discovery compartido de stores de agentes de IA (GAP-01).

Mecanismo agnóstico para localizar los stores de los agentes (gemini-cli,
opencode, hermes) sin hardcodear rutas por fuente. Tres piezas:

- ``discover_chats_dirs``: enumera los dirs de chats de gemini-cli desde el
  ``projects.json`` real (``~/.gemini/projects.json`` → ``{projects: {slug:
  path}}``; los chats viven en ``~/.gemini/tmp/<slug>/chats``). Fail-open:
  archivo ausente o corrupto → lista vacía (nunca crash).
- ``normalize_store_path``: prioridad env override > config del agente
  (key ``data``) > default. Fail-open: config ausente/corrupto → default.
- ``coverage_gaps``: dirs de chats con sesiones sin cosechar (para el
  warning del daemon). Fail-open: sin cursores → todos son gaps.

Doctrina (Artículo VIII): cero abstracciones con 0-1 implementaciones —
cada función tiene consumidores reales (harvesters gemini/opencode/hermes
y el daemon).
"""

from __future__ import annotations

import json
import os
from glob import glob
from typing import Optional


def _gemini_home() -> str:
    """Home de gemini-cli: ``~/.gemini`` (env override para tests)."""
    return os.path.join(os.path.expanduser("~"), ".gemini")


def discover_chats_dirs(projects_json: Optional[str] = None) -> list[str]:
    """Dirs de chats de gemini-cli para TODOS los proyectos del projects.json.

    El ``projects.json`` real mapea ``{projects: {<abs_path>: <slug>}}``
    (verificado contra el host — la clave es el path del proyecto y el VALOR
    el slug); los chats de cada proyecto viven en
    ``~/.gemini/tmp/<slug>/chats`` (ej. ``~/.gemini/tmp/master/chats``).
    Los slugs cuyo dir de chats no existe (proyecto nunca usado) se omiten.

    Fail-open (Artículo IX — sin teatro): projects.json ausente o corrupto
    → lista vacía, sin excepciones. El caller decide el fallback.
    """
    path = projects_json or os.path.join(_gemini_home(), "projects.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return []
    dirs: list[str] = []
    for slug in projects.values():
        if not isinstance(slug, str) or not slug:
            continue
        chats = os.path.join(_gemini_home(), "tmp", slug, "chats")
        if os.path.isdir(chats):
            dirs.append(chats)
    return dirs


def normalize_store_path(env_var: str, config_file: str, default: str) -> str:
    """Resuelve el path de un store: env override > config > default.

    - ``env_var``: variable de entorno del store (ej. ``CAUSADB_OPENCODE_DB_PATH``).
    - ``config_file``: config del agente (forward-compat); se lee la key
      ``data`` (ej. ``~/.config/opencode/opencode.json`` → ``{"data": ...}``).
    - ``default``: path por defecto del usuario.

    Fail-open: config ausente/corrupto/sin key ``data`` → default (nunca
    crash). El env override SIEMPRE gana (el operador manda).
    """
    env_path = os.environ.get(env_var)
    if env_path:
        return env_path
    try:
        with open(config_file) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default
    if isinstance(data, dict) and isinstance(data.get("data"), str) and data["data"]:
        return data["data"]
    return default


def _slug_of_chats_dir(chats_dir: str) -> str:
    """Slug del store: basename del dir padre (``.../tmp/<slug>/chats``)."""
    return os.path.basename(os.path.dirname(chats_dir)) or "default"


def coverage_gaps(
    chats_dirs: list[str],
    ledger_path: str,
    cursors_path: Optional[str] = None,
) -> list[str]:
    """Dirs de chats con sesiones sin cosechar (gaps de cobertura).

    Un dir está cubierto si TODAS sus sesiones tienen entrada en el cursor
    (clave por basename — legacy — o por ``slug/basename`` — multi-store).
    Fail-open: sin archivo de cursores (o corrupto) → todos los dirs son
    gaps (el daemon avisa; no asume cobertura que no puede verificar).
    """
    if not chats_dirs:
        return []
    cursors_path = cursors_path or os.path.join(
        os.path.dirname(ledger_path), ".harvester_cursors.json"
    )
    try:
        with open(cursors_path) as f:
            cursors = json.load(f)
    except (OSError, json.JSONDecodeError):
        return list(chats_dirs)
    files = {}
    gemini_cursor = cursors.get("agent:gemini") if isinstance(cursors, dict) else None
    if isinstance(gemini_cursor, dict):
        files = gemini_cursor.get("files") or {}

    gaps: list[str] = []
    for chats_dir in chats_dirs:
        slug = _slug_of_chats_dir(chats_dir)
        sessions = glob(os.path.join(chats_dir, "session-*.jsonl"))
        covered = all(
            os.path.basename(s) in files or f"{slug}/{os.path.basename(s)}" in files
            for s in sessions
        )
        if not covered:
            gaps.append(chats_dir)
    return gaps