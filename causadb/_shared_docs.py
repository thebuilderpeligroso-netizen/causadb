"""Anotadores fijos de Coordinación Multi-Agente en Workspace.

Dos documentos fijos en `.causadb/coordination/`:
- AUDIT_REPORT.json — solo Auditor
- ACTION_PLAN.json — solo Coder

Un solo documento que se sobrescribe. El ledger es el historial.
"""
import json
import os
from pathlib import Path
from typing import Dict, Any
from datetime import datetime, timezone


# Nombres fijos — SOLO estos dos
ALLOWED_NAMES = frozenset({"AUDIT_REPORT", "ACTION_PLAN"})
SHARED_DOCS_SUBDIR = "coordination"

# Plantillas por defecto (núcleo fijo + notas_libres)
DEFAULT_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "AUDIT_REPORT": {
        "version": 1,
        "tipo": "AUDIT_REPORT",
        # --- CAMPOS OBLIGATORIOS ---
        "fecha_auditoria": "",           # ISO 8601 - cuándo se realizó
        "auditor": "",                   # quién la hizo (nombre/ID)
        "alcance": "",                   # qué se audita (BIT, módulo, feature)
        "criterio_auditoria": "",        # estándares/reglas usadas (ej: BIT-CHR.xx, Art. II, canon)
        "plan_auditado": "",             # referencia al ACTION_PLAN auditado (ID/título)
        # --- CUERPO DE LA AUDITORÍA ---
        "resumen_ejecutivo": "",         # 3-5 líneas: qué se hizo y veredicto
        "hallazgos": [],                 # lista: {id, severidad, descripcion, evidencia_ref}
        "evidencia": [],                 # refs a ledger/queries/tests: ["causadb_query(...)", "test_X::test_Y"]
        "conclusion": "",                # veredicto final: APROBADO / RECHAZADO / CONDICIONAL
        "recomendaciones": [],           # acciones correctivas/preventivas (distinto de hallazgos)
        # --- METADATOS ---
        "estado": "BORRADOR",            # BORRADOR / APROBADO / RECHAZADO / REQUIERE_CAMBIOS
        "actualizado_por": "",           # quién escribió
        "actualizado_en": "",            # ISO 8601
        "notas_libres": ""               # solo para contexto extra no estructurado
    },
    "ACTION_PLAN": {
        "version": 1,
        "tipo": "ACTION_PLAN",
        # --- CAMPOS OBLIGATORIOS ---
        "objetivo": "",                  # qué se quiere lograr (1 línea)
        "causa_raiz": "",                # causa raíz identificada (5 porqués si aplica)
        "bit_relacionado": "",           # BIT-CHR.xx si aplica
        # --- PLAN ---
        "cambios_propuestos": [],        # lista: {archivo, cambio, justificacion}
        "tests_red": [],                 # tests que DEBEN fallar primero (RED)
        "tests_green": [],               # tests que DEBEN pasar después (GREEN)
        "mutantes_controlados": [],      # mutaciones para validar anti-teatro
        # --- RIESGOS Y DECISIONES ---
        "riesgos": [],                   # {descripcion, probabilidad, impacto, mitigacion}
        "dependencias": [],              # otros BITs, PRs, tareas previas
        "solicitud_al_auditor": "APROBAR / OBJETAR",
        # --- METADATOS ---
        "estado": "BORRADOR",            # BORRADOR / EN_REVISION / APROBADO / RECHAZADO
        "actualizado_por": "",
        "actualizado_en": "",
        "notas_libres": ""
    }
}


def _get_coord_dir(ledger_path: str) -> Path:
    """Directorio .causadb/coordination/ a partir del ledger_path."""
    ledger_dir = Path(os.path.dirname(ledger_path))
    return Path(os.path.join(ledger_dir, "coordination"))


def _doc_path(ledger_path: str, name: str) -> Path:
    """Ruta completa al documento."""
    return _get_coord_dir(ledger_path) / f"{name}.json"


def _ensure_coord_dir(ledger_path: str) -> None:
    """Crea .causadb/coordination/ si no existe."""
    _get_coord_dir(ledger_path).mkdir(parents=True, exist_ok=True)


def get_template(name: str) -> dict:
    """Retorna plantilla por defecto (copia profunda)."""
    if name not in ALLOWED_NAMES:
        raise ValueError(f"Nombre no permitido: {name}. Permitidos: {ALLOWED_NAMES}")
    return DEFAULT_TEMPLATES[name].copy()


def ensure_shared_docs(ledger_path: str) -> None:
    """Crea directorio y escribe plantillas si no existen (no sobrescribe)."""
    _ensure_coord_dir(ledger_path)
    for name in ALLOWED_NAMES:
        path = _doc_path(ledger_path, name)
        if not path.exists():
            template = get_template(name)
            _atomic_write(path, template)


def read_shared_doc(ledger_path: str, name: str) -> dict:
    """Lee documento compartido. Si no existe, retorna plantilla vacía."""
    if name not in ALLOWED_NAMES:
        raise ValueError(f"Nombre no permitido: {name}. Permitidos: {ALLOWED_NAMES}")
    path = _doc_path(ledger_path, name)
    if not path.exists():
        return get_template(name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return get_template(name)


def write_shared_doc(ledger_path: str, name: str, content: dict) -> None:
    """Escribe documento (atomic write + fsync). Valida nombre y estructura mínima."""
    if name not in ALLOWED_NAMES:
        raise ValueError(f"Nombre no permitido: {name}. Permitidos: {ALLOWED_NAMES}")
    # Validación mínima de estructura
    if "tipo" not in content:
        raise ValueError("Campo 'tipo' obligatorio y debe coincidir con el nombre")
    if content.get("tipo") != name:
        raise ValueError("Campo 'tipo' obligatorio y debe coincidir con el nombre")
    content["actualizado_en"] = _now_iso()
    _atomic_write(_doc_path(ledger_path, name), content)


def _atomic_write(path: Path, data: dict) -> None:
    """Escritura atómica + fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()