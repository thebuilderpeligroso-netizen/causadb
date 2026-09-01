"""F1.3 — Generación del CODEBASE_ARCHITECTURE_SNAPSHOT (artefacto estático).

NO entra al DAG (decisión del auditor): es un artefacto estático, no causal.
Se emite como EVENTO CODEBASE_ARCHITECTURE_SNAPSHOT + ``$blob`` en BlobStore.

Por defecto parsea con ``ast`` (stdlib): extrae imports + calls de cada
archivo .py, resuelve imports intra-paquete (lookup de dict), computa
clusters por prefijo de paquete y fan-in a nivel archivo.

Auto-detección de codebase-memory con degradación suave: si el binario
``codebase-memory`` está en PATH, ``generator="codebase-memory"`` (señalado,
no invocado con detalle fino); si no, ``generator="ast"``.

Todo envuelto en try/except: cualquier fallo degrada a un snapshot mínimo
(no rompe el import).
"""

import ast
import datetime
import os
import shutil
import uuid

# Directorios que nunca se indexan (evita .causadb/.git/.venv en el snapshot).
_EXCLUDED_DIRS = {
    ".causadb", ".git", "__pycache__", ".venv", "venv",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _detect_codebase_memory() -> bool:
    """True si el binario ``codebase-memory`` está en PATH."""
    try:
        return shutil.which("codebase-memory") is not None
    except Exception:
        return False


def _iter_py_files(project_path: str):
    """Itera los archivos .py del proyecto, excluyendo dirs de ruido."""
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _module_path_for(file_path: str, project_path: str) -> str:
    """Ruta de módulo (pkg.mod) para un archivo .py del proyecto."""
    rel = os.path.relpath(file_path, project_path).replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def _extract_imports(tree) -> list:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
                # `from pkg import mod_b` → también "pkg.mod_b" (ruta completa)
                for alias in node.names:
                    if alias.name != "*":
                        imports.append(f"{node.module}.{alias.name}")
    return imports


def _extract_calls(tree) -> list:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
    return calls


def generate_codebase_snapshot(project_path: str, project_id: str | None = None) -> dict:
    """Genera un snapshot de arquitectura del proyecto.

    Returns:
        dict con ``project_id``, ``generated_at``, ``generator``, ``nodes``,
        ``edges``, ``clusters`` y ``fan_in``. Nunca lanza: cualquier fallo
        degrada a un snapshot mínimo.
    """
    project_path = os.path.abspath(project_path)
    project_id = project_id or str(uuid.uuid4())
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        generator = "codebase-memory" if _detect_codebase_memory() else "ast"
        nodes = []
        edges = []
        module_to_file = {}
        file_imports = {}

        for file_path in _iter_py_files(project_path):
            rel = os.path.relpath(file_path, project_path).replace(os.sep, "/")
            module = _module_path_for(file_path, project_path)
            module_to_file[module] = rel
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
                tree = ast.parse(src)
                imports = _extract_imports(tree)
                calls = _extract_calls(tree)
            except Exception:
                imports, calls = [], []
            file_imports[rel] = imports
            nodes.append({"id": rel, "type": "file", "imports": imports, "calls": calls})

        # Resolver imports intra-paquete (lookup de dict, ~99.6% resuelven).
        for rel, imports in file_imports.items():
            for imp in imports:
                target = module_to_file.get(imp)
                if target is None:
                    parts = imp.split(".")
                    for i in range(len(parts), 0, -1):
                        candidate = ".".join(parts[:i])
                        if candidate in module_to_file:
                            target = module_to_file[candidate]
                            break
                if target is not None and target != rel:
                    edges.append({"source": rel, "target": target, "type": "import"})

        # Clusters por prefijo de paquete (primer segmento de la ruta).
        clusters = {}
        for node in nodes:
            top = node["id"].split("/")[0]
            clusters.setdefault(top, []).append(node["id"])

        # Fan-in a nivel archivo (cuántos archivos lo importan).
        fan_in = {}
        for edge in edges:
            fan_in[edge["target"]] = fan_in.get(edge["target"], 0) + 1

        return {
            "project_id": project_id,
            "generated_at": generated_at,
            "generator": generator,
            "nodes": nodes,
            "edges": edges,
            "clusters": clusters,
            "fan_in": fan_in,
        }
    except Exception:
        # Degradación suave: snapshot mínimo, no rompe el import.
        return {
            "project_id": project_id,
            "generated_at": generated_at,
            "generator": "ast",
            "nodes": [],
            "edges": [],
            "clusters": {},
            "fan_in": {},
        }