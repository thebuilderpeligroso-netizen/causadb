"""Genera el fixture de OpenJarvis: ``tests/fixtures/openjarvis_fixture.db``.

Copia FIEL del store real ``~/.openjarvis/traces.db`` (Artículo IX — datos
reales, no mocks), verificada el 2026-08-02: 7 traces + 7 trace_steps,
todos ``step_type='respond'``, modelos ``qwen2.5-coder:14b`` (5) y
``ornith:9b`` (2), engine ``ollama``.

El db real tiene un **WAL activo** (los datos viven en el ``-wal``, el
archivo main es de 4KB) → NO se copian los 3 archivos a mano (una copia
ciega del main file daría un db vacío). En su lugar se usa
``VACUUM INTO``: SQLite hace checkpoint del WAL a un archivo único limpio
con TODOS los datos (tablas, índices, FTS incluidos). Si ``VACUUM INTO``
fallara (permisos/versión), la alternativa documentada es copiar
``traces.db`` + ``-wal`` + ``-shm`` y abrir/leer la copia — pero esa copia
sigue necesitando los side-files, por lo que se prefiere VACUUM INTO.

El builder NO toca el db real de OpenJarvis (abre en ``mode=ro`` y el
VACUUM INTO escribe a un archivo nuevo). Es re-ejecutable: regenera el
fixture desde el store real, verificando los COUNTs (7/7) antes de darlo
por bueno.

Re-ejecutar para regenerar: ``python tests/fixtures/_build_openjarvis_fixture.py``
"""

import os
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "openjarvis_fixture.db")

EXPECTED_TRACES = 7
EXPECTED_STEPS = 7


def _source_db_path() -> str:
    env_path = os.environ.get("CAUSADB_OPENJARVIS_DB_PATH")
    if env_path:
        return env_path
    return os.path.expanduser("~/.openjarvis/traces.db")


def _build() -> None:
    src = _source_db_path()
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"Store real de OpenJarvis no encontrado: {src}. "
            "No se puede regenerar la fixture (Artículo IX: datos reales)."
        )

    # -- VACUUM INTO: checkpoint del WAL a un archivo único limpio --------
    # Se escribe primero a un temp (mismo fs) y luego se mueve al destino:
    # si algo falla a mitad, el fixture existente no queda corrupto.
    fd, tmp_path = tempfile.mkstemp(suffix=".db.tmp", dir=HERE)
    os.close(fd)
    os.remove(tmp_path)  # VACUUM INTO exige que el destino NO exista

    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute(f"VACUUM INTO '{tmp_path}'")
    finally:
        con.close()

    # -- Verificación de COUNTs antes de reemplazar el fixture -------------
    ro = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
    try:
        n_traces = ro.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        n_steps = ro.execute("SELECT COUNT(*) FROM trace_steps").fetchone()[0]
        max_rowid = ro.execute("SELECT MAX(id) FROM trace_steps").fetchone()[0]
        step_types = [
            r[0] for r in ro.execute("SELECT DISTINCT step_type FROM trace_steps")
        ]
        models = sorted(
            r[0] for r in ro.execute(
                "SELECT DISTINCT tr.model FROM trace_steps ts "
                "LEFT JOIN traces tr ON tr.trace_id = ts.trace_id"
            )
        )
    finally:
        ro.close()

    if n_traces != EXPECTED_TRACES or n_steps != EXPECTED_STEPS:
        os.remove(tmp_path)
        raise AssertionError(
            f"Fixture inválida: {n_traces} traces / {n_steps} steps "
            f"(esperaba {EXPECTED_TRACES}/{EXPECTED_STEPS}). "
            "El store real cambió — revisar antes de regenerar."
        )

    os.replace(tmp_path, OUT)
    print(
        f"fixture OK: {OUT} ({n_traces} traces, {n_steps} trace_steps, "
        f"max_rowid={max_rowid}, step_types={step_types}, models={models})"
    )


if __name__ == "__main__":
    _build()
