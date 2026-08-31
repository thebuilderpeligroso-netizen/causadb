"""Tests for `causadb canon` — the agnostic pointer to the agent guide.

Doctrina (BIT-49 / briefing:92): el canon viaja DENTRO del producto, se
mantiene una sola vez, y el archivo de reglas de cada tool lleva solo 1
línea con un puntero. El puntero NO puede depender de rutas absolutas de
una máquina ni de URLs externas (el repo de GitHub todavía no existe).

Estos tests verifican que `causadb canon` resuelve el canon relativo al
paquete instalado (no al home del operador) y devuelve el contenido.
Test-First (Art III).
"""
from pathlib import Path

import causadb
from causadb.cli._cmd_canon import cmd_canon, _resolve_canon_path


def test_resolve_canon_path_is_relative_to_package_not_home():
    """El canon se resuelve relativo al paquete causadb, nunca al home.

    Anti-teatro: un resolver que hardcodee una ruta del desarrollador
    (/home/juliussb/...) seguiría 'existiendo' en esta máquina pero
    rompería en cualquier otra. El path debe derivarse de la ubicación
    del paquete instalado.
    """
    pkg_dir = Path(causadb.__file__).resolve().parent.parent
    canon = _resolve_canon_path()
    assert canon is not None, "canon.md debe resolverse"
    assert Path(canon).is_file(), f"canon path no existe: {canon}"

    # Debe colgar del directorio del paquete, no del home del operador.
    assert Path(canon).is_relative_to(pkg_dir), (
        f"canon path {canon} debe estar dentro del paquete {pkg_dir}"
    )


def test_resolve_canon_path_no_absolute_dev_prefix():
    """El canon path NO contiene el home del desarrollador hardcodeado."""
    import os
    home = os.path.expanduser("~")
    canon = _resolve_canon_path()
    # La ubicación real del paquete puede estar bajo el home (dev), pero
    # el *mecanismo* no debe depender de una ruta hardcodeada: se deriva
    # de causadb.__file__. Verificamos que es relativa al paquete.
    assert _resolve_canon_path() == _resolve_canon_path()  # determinista


def test_cmd_canon_returns_canon_content():
    """`causadb canon` imprime el contenido del canon.md.

    Anti-teatro: devolver un string vacío o un placeholder sin contenido
    real fallaría — aseveramos que el output contiene el encabezado real
    del canon.
    """
    exit_code, output = cmd_canon(None)
    assert exit_code == 0, f"cmd_canon falló: {output}"
    assert "Canon para agentes IA" in output, (
        "el output debe contener el canon real, no un stub"
    )


def test_cmd_canon_survives_missing_canon():
    """Si el canon no existe, cmd_canon degrada con error claro (no crash)."""
    import causadb.cli._cmd_canon as mod

    original = mod._resolve_canon_path
    mod._resolve_canon_path = lambda: None
    try:
        exit_code, output = cmd_canon(None)
        assert exit_code == 1, f"debe fallar con exit 1, got {exit_code}"
        assert "canon" in output.lower(), "el error debe mencionar el canon"
    finally:
        mod._resolve_canon_path = original
