"""Compatibilidad MCP v1/v2 (BIT-CHR: mcp 1.28.1 Linux / mcp 2.1.1 Windows).

Verifica que server.py expone `MCPBase` (el alias de la clase base según la
versión instalada) y que los type hints de `create_server()` usan `MCPBase`
y NO `FastMCP` (que en v2 no queda importado y lanzaría NameError al evaluar
la anotación — server.py no tiene `from __future__ import annotations`).

También cubre el helper central `_error_message` (tests/helpers/_mcp_call.py):
el ValueError original va en `__cause__` (raise ... from e) en ambas versiones.
"""
import pytest

from causadb.mcp.server import create_server
from tests.helpers._mcp_call import _call_tool, _error_message


def test_server_module_exports_mcpbase():
    """server.py debe exponer `MCPBase` (alias v1/v2).

    RED: antes del cambio, `MCPBase` no existe en el módulo → ImportError.
    GREEN: tras el cambio, `MCPBase` es la clase base de la versión instalada.
    """
    from causadb.mcp import server as server_mod
    assert hasattr(server_mod, "MCPBase"), (
        "server.py debe exponer MCPBase (alias de la clase base v1/v2)"
    )


def test_create_server_return_annotation_is_mcpbase():
    """El type hint de retorno de create_server() debe resolver a MCPBase.

    RED: antes del cambio la anotación es `FastMCP` (NameError en v2).
    GREEN: tras el cambio la anotación resuelve a `server_mod.MCPBase`
    (que en Linux es FastMCP y en Windows es MCPServer).

    Anti-teatro: además de la anotación funcional, verificamos que el
    SOURCE no contenga `-> FastMCP` (que rompería en v2 por NameError).
    """
    from causadb.mcp import server as server_mod
    ann = create_server.__annotations__.get("return")
    assert ann is not None, "create_server() debe tener anotación de retorno"
    assert ann is server_mod.MCPBase, (
        f"create_server() debe anotar -> MCPBase, got {ann}"
    )
    # server.py NO tiene `from __future__ import annotations`, así que la
    # anotación se evalúa al definir la función. Si quedara `-> FastMCP`
    # (no importado en v2) lanzaría NameError. Verificamos el source.
    import inspect
    src = inspect.getsource(server_mod.create_server)
    assert "-> MCPBase" in src, "create_server() debe anotar -> MCPBase en el source"
    assert "-> FastMCP" not in src, "no debe quedar -> FastMCP en el source"


def test_create_server_returns_instance_of_mcpbase():
    """create_server() retorna una instancia de la clase base de esta versión.

    En Linux (mcp 1.28.1) MCPBase es FastMCP; en Windows (mcp 2.1.1) es
    MCPServer. El test es agnóstico: solo exige que la instancia sea de
    MCPBase (la clase que server.py importa).
    """
    from causadb.mcp import server as server_mod
    server = create_server()
    assert isinstance(server, server_mod.MCPBase), (
        "create_server() debe retornar una instancia de MCPBase"
    )


def test_error_message_returns_cause_when_present():
    """_error_message devuelve el mensaje del `__cause__` (ValueError original)."""
    original = ValueError("ledger path")
    wrapped = RuntimeError("ToolError wrapper")
    wrapped.__cause__ = original
    assert _error_message(wrapped) == "ledger path"


def test_error_message_returns_str_when_no_cause():
    """_error_message devuelve str(exc) cuando no hay `__cause__`."""
    exc = ValueError("session_id or search")
    assert _error_message(exc) == "session_id or search"


def test_error_message_returns_str_when_cause_is_none():
    """_error_message con `__cause__` None explícito → str(exc)."""
    exc = ValueError("plain message")
    exc.__cause__ = None
    assert _error_message(exc) == "plain message"