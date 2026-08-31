"""Tests for the `causadb://canon` MCP resource.

Doctrina (BIT-49 / briefing:92): el canon viaja DENTRO del producto. Un
agente MCP lo lee sin conocer rutas de archivo — el resource lo expone de
forma agnóstica a cualquier tool. Test-First (Art III).
"""
import anyio

from causadb.mcp.server import create_server


def _read_resource(server, uri: str):
    async def _run():
        contents = await server.read_resource(uri)
        return contents[0].content
    return anyio.run(_run)


def _list_resource_uris(server):
    async def _run():
        resources = await server.list_resources()
        return {str(r.uri) for r in resources}
    return anyio.run(_run)


def test_canon_resource_registered():
    """list_resources debe incluir `causadb://canon`.

    Anti-teatro: un server sin el @mcp.resource() para canon fallaría
    porque la URI estaría ausente.
    """
    server = create_server()
    uris = _list_resource_uris(server)
    assert "causadb://canon" in uris, (
        f"causadb://canon not found in resource URIs: {uris}"
    )


def test_canon_resource_returns_canon_content():
    """causadb://canon devuelve el contenido real del canon.

    Anti-teatro: un resource stub devolviendo un placeholder fallaría —
    aseveramos que el contenido contiene el encabezado real del canon.
    """
    server = create_server()
    text = _read_resource(server, "causadb://canon")
    assert "Canon para agentes IA" in text, (
        "el resource canon debe devolver el canon real, no un stub"
    )
    assert "## 3. Cómo reconstruir el estado previo" in text, (
        "el canon debe incluir la escalera de reconstrucción"
    )
