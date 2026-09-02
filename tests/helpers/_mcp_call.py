"""Shim de compatibilidad MCP v1/v2 para los tests.

Centraliza los helpers `_call_tool` / `_error_message` que antes estaban
duplicados en 6 archivos de test (decisión del operador).

Ramas soportadas:
  - mcp v1 (Linux / mcp 1.28.1, FastMCP): `call_tool` devuelve tupla
    `(content_blocks, structured)`.
  - mcp v2 (Windows / mcp 2.1.1, MCPServer): `call_tool` devuelve un objeto
    `CallToolResult` con `.content` / `.structured_content`.

En ambas versiones el ValueError original va en `__cause__` (raise ... from e),
por eso `_error_message` lo desenvuelve.
"""
import anyio


def _call_tool(server, name, arguments):
    """Invoca una tool normalizando el retorno v1 (tupla) / v2 (CallToolResult)."""
    async def _run():
        result = await server.call_tool(name, arguments)
        if isinstance(result, tuple):
            content_blocks, structured = result   # Rama v1 (Linux / mcp 1.28.1)
        else:
            content_blocks = result.content       # Rama v2 (CallToolResult)
            structured = result.structured_content
        return content_blocks, structured
    return anyio.run(_run)


def _error_message(exc):
    """Mensaje de un error envuelto por MCP (v1 ToolError / v2 UnexpectedToolError).

    El ValueError original va en __cause__ (raise ... from e) en ambas versiones.
    """
    return str(exc.__cause__) if exc.__cause__ else str(exc)