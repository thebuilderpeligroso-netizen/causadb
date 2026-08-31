"""F.13.4.1 / F.13.4.2 — Context Profiler + Distill Engine (Skills/Distill).

Analiza eventos del ledger para detectar patrones repetidos y estimar
el tamaño del contexto actual (F.13.4.1), y produce "skills" comprimidos
a partir del profile (F.13.4.2).

REGLA CRÍTICA (roadmap F.13.4.1 / F.13.4.2):
    Redactar prompts antes de analizar: pasar por ``redact_payload()`` de
    ``_redactor.py`` antes de extraer patrones de ``LLM_INVOKED.payload.prompt``
    o de ``REASONING_STEP.payload``. Skills producidos de payloads no
    redactados son rechazados por el validador.

Defensa en profundidad: aunque ``LedgerWriter`` ya redacta al escribir, este
módulo redacta de nuevo al leer. No todos los ledgers se escriben vía
``LedgerWriter`` (imports, migraciones, fuentes externas), y el principio de
seguridad exige redactar en el punto de uso, no confiar en el de escritura.

Artículo V (no cargar al modelo): el skill ``decisions`` solo incluye el
``step_hash`` (referencia), nunca el contenido completo del razonamiento.
El hash permite trazabilidad sin exponer el contexto al modelo.
"""

import json
from collections import Counter
from typing import Any, Dict, List, Optional

from causadb._config import CausaDBConfig
from causadb._ledger_reader import LedgerReader
from causadb._redactor import redact_payload, SENSITIVE_FIELDS


def _extract_file_paths(payload: Dict[str, Any]) -> List[str]:
    """Extrae todos los file paths de un payload de FILE_MODIFIED.

    Soporta ``payload.path`` (string), ``payload.writes`` (list de paths o
    dicts con key ``path``), y ``payload.reads`` (mismo formato).
    """
    paths: List[str] = []
    # path directo
    p = payload.get("path")
    if isinstance(p, str):
        paths.append(p)
    # writes / reads: pueden ser listas de strings o listas de dicts
    for key in ("writes", "reads"):
        val = payload.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    paths.append(item)
                elif isinstance(item, dict):
                    ip = item.get("path")
                    if isinstance(ip, str):
                        paths.append(ip)
    return paths


def _detect_redacted_fields(original: Dict[str, Any], redacted: Dict[str, Any]) -> List[str]:
    """Detecta qué fields cambiaron al redactar (transparencia).

    Compara el payload original con el redactado y reporta las keys
    cuyo valor cambió — esas son las que fueron redactadas.
    """
    changed: List[str] = []
    for key in set(list(original.keys()) + list(redacted.keys())):
        if key.lower() in SENSITIVE_FIELDS and original.get(key) != redacted.get(key):
            changed.append(key)
    return changed


def profile_context(ledger_path: str, config: Optional[CausaDBConfig] = None) -> Dict[str, Any]:
    """Analyze ledger events to detect recurring patterns.

    Returns a profile dict with:
    - total_tokens: int — approximate tokens of context (sum of prompt chars / 4)
    - unique_files: int — number of distinct file paths touched
    - unique_tools: int — number of distinct tool_names used
    - repetition_ratio: float 0-1 — ratio of repeated patterns vs unique
    - top_patterns: list[dict] — top 5 repeating patterns with count
    - redacted_fields: list[str] — fields that were redacted (transparency)

    CRITICAL: redacts LLM_INVOKED.payload.prompt via _redactor.redact_payload
    BEFORE extracting patterns. Skills from unredacted prompts are rejected.
    """
    config = config or CausaDBConfig(ledger_path=ledger_path)

    reader = LedgerReader(ledger_path)
    entries = list(reader.read_all_entries())

    # --- Paso 1: Redactar prompts ANTES de analizar (defensa en profundidad) ---
    redacted_fields: List[str] = []
    events: List[Dict[str, Any]] = []
    for entry in entries:
        event = entry["event"]
        payload = event.get("payload", {})
        if event.get("event_type") == "LLM_INVOKED" and isinstance(payload, dict):
            original_payload = dict(payload)
            redacted_payload = redact_payload(original_payload, config)
            changed = _detect_redacted_fields(original_payload, redacted_payload)
            for f in changed:
                if f not in redacted_fields:
                    redacted_fields.append(f)
            event = dict(event)
            event["payload"] = redacted_payload
        events.append(event)

    # --- Paso 2: Analizar patrones ---
    file_counter: Counter = Counter()
    tool_counter: Counter = Counter()
    total_prompt_chars = 0

    for ev in events:
        etype = ev.get("event_type")
        payload = ev.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if etype == "FILE_MODIFIED":
            for fp in _extract_file_paths(payload):
                file_counter[fp] += 1

        elif etype == "TOOL_CALLED":
            tn = payload.get("tool_name")
            if isinstance(tn, str):
                tool_counter[tn] += 1

        elif etype == "LLM_INVOKED":
            prompt = payload.get("prompt")
            if isinstance(prompt, str):
                total_prompt_chars += len(prompt)

    # --- Paso 3: Calcular métricas ---
    unique_files = len(file_counter)
    unique_tools = len(tool_counter)

    total_file_refs = sum(file_counter.values())
    total_tool_refs = sum(tool_counter.values())
    total_refs = total_file_refs + total_tool_refs
    total_unique = unique_files + unique_tools

    if total_refs > 0:
        repetition_ratio = 1.0 - (total_unique / total_refs)
    else:
        repetition_ratio = 0.0

    # top_patterns: combinar files y tools, ordenar por count desc, top 5
    all_patterns: List[Dict[str, Any]] = []
    for fp, count in file_counter.items():
        all_patterns.append({"pattern": fp, "count": count, "type": "file"})
    for tn, count in tool_counter.items():
        all_patterns.append({"pattern": tn, "count": count, "type": "tool"})
    all_patterns.sort(key=lambda x: x["count"], reverse=True)
    top_patterns = all_patterns[:5]

    # total_tokens: aproximación grosera 4 chars ≈ 1 token
    total_tokens = total_prompt_chars // 4

    return {
        "total_tokens": total_tokens,
        "unique_files": unique_files,
        "unique_tools": unique_tools,
        "repetition_ratio": repetition_ratio,
        "top_patterns": top_patterns,
        "redacted_fields": redacted_fields,
    }


# ============================================================================
# F.13.4.2 — Distill Engine
# ============================================================================


def _build_file_tree_content(file_paths: List[str]) -> str:
    """Agrupa file paths por directorio y produce un árbol de texto simple.

    Formato:
        dir1/
        - file_a.py
        - file_b.py
        dir2/sub/
        - file_c.py
        (root)
        - root_file.py
    """
    if not file_paths:
        return ""

    # Agrupar por directorio preservando orden de aparición.
    grouped: Dict[str, List[str]] = {}
    dir_order: List[str] = []
    for fp in file_paths:
        # Normalizar separadores a '/' (los paths del ledger pueden venir en
        # cualquier formato, pero asumimos '/' como canónico).
        norm = fp.replace("\\", "/")
        if "/" in norm:
            dir_part, file_part = norm.rsplit("/", 1)
        else:
            dir_part, file_part = "", norm
        if dir_part not in grouped:
            grouped[dir_part] = []
            dir_order.append(dir_part)
        if file_part not in grouped[dir_part]:
            grouped[dir_part].append(file_part)

    lines: List[str] = []
    for dir_part in dir_order:
        files = grouped[dir_part]
        if dir_part == "":
            lines.append("(root)")
        else:
            lines.append(f"{dir_part}/")
        for f in files:
            lines.append(f"- {f}")
    return "\n".join(lines)


def _build_tool_patterns_content(top_patterns: List[Dict[str, Any]]) -> str:
    """Construye el content del skill tool_patterns desde top_patterns.

    Solo incluye patrones de tipo "tool" con count > 1 (repeticiones
    significativas; count == 1 no es un patrón).
    """
    lines: List[str] = []
    for p in top_patterns:
        if p.get("type") == "tool" and p.get("count", 0) > 1:
            lines.append(f"tool '{p['pattern']}' used {p['count']} times")
    return "\n".join(lines)


def _build_decisions_content(ledger_path: str, config: CausaDBConfig) -> str:
    """Relee el ledger, filtra REASONING_STEP con step_type='decision',
    redacta el payload, y devuelve lines con 'Decision: <step_hash>'.

    Artículo V: solo el step_hash (referencia), nunca el contenido completo.
    """
    reader = LedgerReader(ledger_path)
    lines: List[str] = []
    for entry in reader.read_all_entries():
        event = entry["event"]
        if event.get("event_type") != "REASONING_STEP":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if payload.get("step_type") != "decision":
            continue
        # Redactar ANTES de extraer (defensa en profundidad).
        redacted = redact_payload(payload, config)
        step_hash = redacted.get("step_hash", "")
        lines.append(f"Decision: {step_hash}")
    return "\n".join(lines)


def _build_governance_content(ledger_path: str, config: CausaDBConfig) -> str:
    """Relee el ledger, filtra GOVERNANCE_DECISION events, estructura contenido.

    Read-only: solo LEE eventos existentes, no escribe nada.
    Artículo V: reasoning se redacta antes de incluir (defensa en profundidad).

    Returns:
        String con hasta 10 decisiones, cada reasoning truncado a 500 chars.
    """
    reader = LedgerReader(ledger_path)
    lines: List[str] = []
    for entry in reader.read_all_entries():
        event = entry["event"]
        if event.get("event_type") != "GOVERNANCE_DECISION":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        # Redactar antes de extraer (defensa en profundidad)
        redacted = redact_payload(payload, config)
        reasoning = redacted.get("reasoning", "")
        impact = redacted.get("impact", "unknown")
        decision_type = redacted.get("decision_type", "unknown")
        origin = redacted.get("origin", "unknown")

        # Truncar cada reasoning a 500 chars
        if len(reasoning) > 500:
            reasoning = reasoning[:497] + "..."

        line = f"[{decision_type}/{impact}] ({origin}) {reasoning}"
        lines.append(line)

        # Max 10 decisiones en el skill
        if len(lines) >= 10:
            break

    return "\n".join(lines)


def distill(ledger_path: str, config: Optional[CausaDBConfig] = None) -> Dict[str, Any]:
    """Produce compressed skills from the context profile.

    Returns: ``{skills: [{type, name, content, token_count, confidence}]}``.

    Skill types producidos:
        - ``file_tree``: árbol de archivos tocados (factual, confidence=1.0).
        - ``tool_patterns``: tools repetidos (count > 1) con confidence
          proporcional a la repetición.
        - ``decisions``: hashes de REASONING_STEP con step_type='decision',
          redactados antes de extraer. Solo el hash (Artículo V: no cargar al
          modelo). confidence=0.7 (decisions son context-dependent).
        - ``conventions``: PLACEHOLDER. La detección real de imports/naming/lint
          requiere leer archivos del workspace, lo cual es work de F.13.4.x
          posterior. Por ahora content="Conventions detection not yet available
          (F.13.4.x future work)", token_count=10, confidence=0.0. NO debe
          usarse como skill real hasta que se implemente la detección.
        - ``governance``: decisiones de governance desde GOVERNANCE_DECISION
          events existentes (read-only, no escribe). Contiene hasta 10 decisiones
          con reasoning truncado a 500 chars. confidence=1.0 (datos factuales).

    Compresión: ``sum(skill.token_count)`` debe ser < ``profile.total_tokens``
    cuando el ledger tiene prompts significativos. Si el ledger está vacío,
    ``skills`` es una lista vacía (no hay nada que destilar).

    Anti-teatro (Artículo IX): si se muta ``distill`` para retornar
    ``{"skills": []}`` sobre un ledger no vacío, el test de compresión falla
    porque no hay skills para validar la compresión.
    """
    config = config or CausaDBConfig(ledger_path=ledger_path)

    profile = profile_context(ledger_path, config)

    # Pre-detectar decisions: releer el ledger para saber si hay REASONING_STEP
    # con step_type='decision'. Lo hacemos antes del guard de "ledger vacío"
    # porque un ledger puede tener solo decisions (sin files/tools/prompts) y
    # aún así debe producir un skill decisions.
    dec_content = _build_decisions_content(ledger_path, config)

    # Pre-detectar governance decisions: leer GOVERNANCE_DECISION events
    # (read-only, no escribe).
    gov_content = _build_governance_content(ledger_path, config)

    # Si el ledger no tiene nada de contenido aprovechable, no hay skills.
    # Consideramos "vacío" cuando no hay files, no hay tools, no hay tokens
    # de prompts, y no hay decisions/governance. En ese caso retornamos skills=[].
    has_files = profile["unique_files"] > 0
    has_tools = profile["unique_tools"] > 0
    has_tokens = profile["total_tokens"] > 0
    has_decisions = bool(dec_content)
    has_governance = bool(gov_content)
    if not (has_files or has_tools or has_tokens or has_decisions or has_governance):
        return {"skills": []}

    skills: List[Dict[str, Any]] = []

    # --- A) file_tree skill ---
    # Reconstruir la lista de unique file paths desde el ledger (preservando
    # orden de aparición). top_patterns del profile está limitado a 5, pero
    # para el file_tree queremos TODOS los files únicos.
    reader = LedgerReader(ledger_path)
    unique_files_ordered: List[str] = []
    seen_files: set = set()
    for entry in reader.read_all_entries():
        event = entry["event"]
        if event.get("event_type") != "FILE_MODIFIED":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        for fp in _extract_file_paths(payload):
            if fp not in seen_files:
                seen_files.add(fp)
                unique_files_ordered.append(fp)

    if unique_files_ordered:
        ft_content = _build_file_tree_content(unique_files_ordered)
        if ft_content:
            skills.append({
                "type": "file_tree",
                "name": "touched_files_tree",
                "content": ft_content,
                "token_count": max(1, len(ft_content) // 4),
                "confidence": 1.0,
            })

    # --- B) tool_patterns skill ---
    tp_content = _build_tool_patterns_content(profile["top_patterns"])
    if tp_content:
        # confidence = min(1.0, max_count / 10) donde max_count es la mayor
        # repetición entre los tool patterns incluidos.
        max_count = max(
            (p["count"] for p in profile["top_patterns"]
             if p.get("type") == "tool" and p.get("count", 0) > 1),
            default=0,
        )
        tp_confidence = min(1.0, max_count / 10.0) if max_count > 0 else 0.0
        skills.append({
            "type": "tool_patterns",
            "name": "repeated_tools",
            "content": tp_content,
            "token_count": max(1, len(tp_content) // 4),
            "confidence": tp_confidence,
        })

    # --- C) decisions skill ---
    if dec_content:
        skills.append({
            "type": "decisions",
            "name": "decision_hashes",
            "content": dec_content,
            "token_count": max(1, len(dec_content) // 4),
            "confidence": 0.7,
        })

    # --- D) conventions skill (PLACEHOLDER) ---
    # NOTA: detección real de imports/naming/lint requiere leer archivos del
    # workspace, lo cual es work de F.13.4.x posterior. Este skill es
    # placeholder explícito: confidence=0.0 señala que NO debe usarse como
    # contexto real hasta que se implemente la detección.
    skills.append({
        "type": "conventions",
        "name": "conventions_placeholder",
        "content": "Conventions detection not yet available (F.13.4.x future work)",
        "token_count": 10,
        "confidence": 0.0,
    })

    # --- E) governance skill (read-only, desde GOVERNANCE_DECISION events) ---
    if has_governance:
        skills.append({
            "type": "governance",
            "name": "governance_decisions",
            "content": gov_content,
            "token_count": max(1, len(gov_content) // 4),
            "confidence": 1.0,  # Datos factuales del ledger (Artículo IX)
        })

    return {"skills": skills}
