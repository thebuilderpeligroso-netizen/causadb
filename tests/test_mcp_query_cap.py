"""Tests for BIT-CHR.35 P3 — Cap de outputs en MCP query/replay.

Test-First (Art. III): estos tests se escriben ANTES de la implementación.
Verifican que:

1. ``limit`` respeta el cap (pedir más que el cap devuelve ≤ cap).
2. El default no explota (query sin filtros no devuelve todo la lista
   completa cuando el ledger excede el cap).
3. ``include_payloads=False`` reduce drásticamente el tamaño en bytes
   (~90% menos) preservando trazabilidad (``content_hash`` o ``$blob``).
4. La reducción se aplica ANTES de resolver blobs: eventos con ``$blob``
   y ``limit`` pequeño no tocan disco de blobs (``BlobStore.get`` no
   es llamado para los eventos truncados).

Anti-teatro (Art. IX): cada test tiene poder discriminatorio — un stub
que ignora ``limit`` o resuelve blobs antes de truncar falla al menos
una aserción.
"""
import json
import os

import pytest
import anyio

from causadb._blob_store import BlobStore
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_index import LedgerIndex, DEFAULT_QUERY_LIMIT, MAX_QUERY_LIMIT
from causadb._ledger_writer import LedgerWriter
from causadb.mcp import _tools
from tests.helpers._mcp_call import _call_tool
from causadb.mcp.server import create_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(content_blocks):
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


def _make_large_payload(idx: int) -> dict:
    """Payload ~40KB para que la diferencia con include_payloads=False sea
    grande y medible (simula el caso real TOOL_CALLED con payloads grandes
    externalizados a $blob). Con 40KB por evento, el overhead fijo del
    evento (~500B) es <2% y la reducción supera 90%."""
    return {
        "path": f"/file/{idx}",
        "action": "create",
        "content": "x" * 40000,
        "idx": idx,
    }


# ---------------------------------------------------------------------------
# 1. Constantes de cap documentadas
# ---------------------------------------------------------------------------

def test_query_limit_constants_are_documented():
    """El módulo ``_ledger_index`` expone ``DEFAULT_QUERY_LIMIT`` y
    ``MAX_QUERY_LIMIT`` con valores razonables (500-1000 range).

    Anti-teatro: un stub sin las constantes falla con AttributeError.
    """
    assert isinstance(DEFAULT_QUERY_LIMIT, int)
    assert isinstance(MAX_QUERY_LIMIT, int)
    assert 500 <= DEFAULT_QUERY_LIMIT <= 1000, (
        f"DEFAULT_QUERY_LIMIT debe estar en [500, 1000], got {DEFAULT_QUERY_LIMIT}"
    )
    assert MAX_QUERY_LIMIT >= DEFAULT_QUERY_LIMIT, (
        f"MAX_QUERY_LIMIT ({MAX_QUERY_LIMIT}) debe ser >= DEFAULT_QUERY_LIMIT "
        f"({DEFAULT_QUERY_LIMIT})"
    )


# ---------------------------------------------------------------------------
# 2. limit respeta el cap (pedir más → devuelve ≤ cap)
# ---------------------------------------------------------------------------

def test_query_limit_clamps_to_cap(tmp_path):
    """``limit`` explícito > MAX_QUERY_LIMIT se clampea a MAX_QUERY_LIMIT
    (no error, no excede el cap).

    Anti-teatro: una impl que ignora ``limit`` devuelve todos los eventos
    (> MAX_QUERY_LIMIT) y falla la aserción de longitud.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    # Escribir más eventos que el cap para forzar el truncamiento.
    n = MAX_QUERY_LIMIT + 50
    for i in range(n):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={"path": f"/f{i}", "action": "create"},
        ))

    index = LedgerIndex(ledger_path)
    # Pedir más que el cap → debe devolver exactamente MAX_QUERY_LIMIT.
    results = index.query(limit=n + 1000)
    assert len(results) == MAX_QUERY_LIMIT, (
        f"pedir limit={n + 1000} debe clampear a {MAX_QUERY_LIMIT}, "
        f"got {len(results)}"
    )


def test_query_limit_explicit_below_cap_respected(tmp_path):
    """``limit`` explícito ≤ cap se respeta exactamente.

    Anti-teatro: una impl que siempre devuelve el cap falla porque pedimos
    menos que el cap.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    for i in range(20):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={"path": f"/f{i}", "action": "create"},
        ))

    index = LedgerIndex(ledger_path)
    results = index.query(limit=5)
    assert len(results) == 5, (
        f"limit=5 debe devolver 5 eventos, got {len(results)}"
    )


# ---------------------------------------------------------------------------
# 3. Default no explota (query sin filtros no devuelve todo)
# ---------------------------------------------------------------------------

def test_query_default_applies_cap(tmp_path):
    """``query()`` sin ``limit`` aplica el cap por defecto — no devuelve
    toda la lista cuando el ledger excede el cap.

    Anti-teatro: una impl sin cap devuelve los n eventos completos y
    falla la aserción (n > DEFAULT_QUERY_LIMIT).
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    n = DEFAULT_QUERY_LIMIT + 100
    for i in range(n):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={"path": f"/f{i}", "action": "create"},
        ))

    index = LedgerIndex(ledger_path)
    results = index.query()  # sin limit → default
    assert len(results) == DEFAULT_QUERY_LIMIT, (
        f"query() sin limit debe devolver {DEFAULT_QUERY_LIMIT} (cap), "
        f"got {len(results)} (ledger tiene {n})"
    )


def test_mcp_query_default_does_not_explode(tmp_path):
    """La tool MCP ``query`` sin ``limit`` no devuelve toda la lista
    cuando el ledger excede el cap.

    Anti-teatro: un stub que ignora el cap devuelve n eventos y falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    n = DEFAULT_QUERY_LIMIT + 50
    for i in range(n):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={"path": f"/f{i}", "action": "create"},
        ))

    server = create_server()
    content_blocks, _ = _call_tool(server, "query", {
        "ledger_path": ledger_path,
    })
    # C1: query devuelve SIEMPRE envelope {"events", "truncated", ...}.
    data = json.loads(_text(content_blocks))
    assert isinstance(data, dict), "query debe devolver envelope (C1)"
    results = data["events"]
    # C1 (byte cap): el cap de conteo (DEFAULT_QUERY_LIMIT) y el cap de
    # bytes (MAX_RESPONSE_BYTES) operan juntos — el que se alcance primero
    # gana. 1050 eventos de ~550B ≈ 570KB > 300KB → truncado por bytes.
    assert data["truncated"] is True, "ledger > cap → truncado"
    assert len(results) <= DEFAULT_QUERY_LIMIT, (
        f"MCP query sin limit debe respetar el cap (≤ {DEFAULT_QUERY_LIMIT}), "
        f"got {len(results)}"
    )
    assert len(results) < n, (
        f"no debe devolver toda la lista (n={n}), got {len(results)}"
    )


# ---------------------------------------------------------------------------
# 4. include_payloads=False reduce drásticamente (~90% menos bytes)
# ---------------------------------------------------------------------------

def test_query_include_payloads_false_reduces_size(tmp_path):
    """``include_payloads=False`` reduce el tamaño serializado en bytes
    en al menos 90% comparado con ``include_payloads=True``.

    Anti-teatro: una impl que ignora el flag produce dos salidas iguales
    y la aserción de reducción falla.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    for i in range(10):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload=_make_large_payload(i),
        ))

    index = LedgerIndex(ledger_path)

    full = index.query(include_payloads=True)
    slim = index.query(include_payloads=False)

    full_bytes = len(json.dumps(full, default=str, sort_keys=True))
    slim_bytes = len(json.dumps(slim, default=str, sort_keys=True))

    assert full_bytes > 0
    assert slim_bytes > 0
    # Al menos 90% de reducción.
    reduction = 1.0 - (slim_bytes / full_bytes)
    assert reduction >= 0.90, (
        f"include_payloads=False debe reducir >=90%, got {reduction:.2%} "
        f"(full={full_bytes}B, slim={slim_bytes}B)"
    )


def test_query_include_payloads_false_preserves_trazability(tmp_path):
    """Con ``include_payloads=False`` los eventos conservan claves de
    trazabilidad (``content_hash``, ``path``, ``action``) para no perder
    identificabilidad, descartando solo el contenido pesado.

    Anti-teatro: una impl que elimina el payload por completo pierde
    content_hash/path y falla la aserción.

    Nota: usamos un payload PEQUEÑO (sin ``content`` pesado) para que
    el writer NO lo externalice a ``$blob`` — así verificamos la rama
    inline de ``_slim_payload``. La rama ``$blob`` se verifica en
    ``test_query_include_payloads_false_skips_blob_resolution``.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    # Payload pequeño con content_hash explícito (simula harvest filesystem
    # metadata sin el contenido crudo — el contenido va en el blob aparte).
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="test",
        payload={
            "path": "/foo",
            "action": "create",
            "content_hash": "abc123def456",
        },
    ))

    index = LedgerIndex(ledger_path)
    slim = index.query(include_payloads=False)
    assert len(slim) == 1
    payload = slim[0]["event"]["payload"]
    # content_hash debe preservarse para trazabilidad.
    assert payload.get("content_hash") == "abc123def456", (
        f"include_payloads=False debe preservar content_hash, got {payload}"
    )
    # path y action también son claves de trazabilidad.
    assert payload.get("path") == "/foo"
    assert payload.get("action") == "create"


# ---------------------------------------------------------------------------
# 5. Reducción ANTES de resolver blobs (no toca disco de blobs)
# ---------------------------------------------------------------------------

def test_query_limit_truncates_before_blob_resolution(tmp_path, monkeypatch):
    """El cap se aplica ANTES de resolver blobs: con ``limit`` pequeño
    sobre eventos ``$blob``, ``BlobStore.get`` no es llamado para los
    eventos truncados.

    Anti-teatro: una impl que resuelve blobs antes de truncar llama a
    ``BlobStore.get`` para todos los eventos y el contador excede el limit.

    Nota: para verificar si los eventos se externalizaron a ``$blob``
    leemos el ledger crudo (``LedgerIndex.query`` con
    ``include_payloads=True`` resuelve los ``$blob`` a contenido inline,
    así que no se ven en el resultado).
    """
    ledger_path = str(tmp_path / "ledger.log")
    blob_dir = str(tmp_path / "blobs")
    os.makedirs(blob_dir, exist_ok=True)

    writer = LedgerWriter(ledger_path)
    large_payload = {"path": "/big", "action": "create", "content": "y" * 5000}

    n_events = 10
    for i in range(n_events):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={**large_payload, "idx": i},
        ))

    # Verificar que los eventos se externalizaron a $blob leyendo crudo.
    with open(ledger_path) as f:
        raw_lines = [json.loads(ln) for ln in f if ln.strip()]
    blob_refs = [
        e["event"]["payload"].get("$blob")
        for e in raw_lines
        if isinstance(e["event"].get("payload"), dict) and "$blob" in e["event"]["payload"]
    ]
    if not blob_refs:
        pytest.skip(
            "El writer no externalizó payloads a $blob en este entorno; "
            "no se puede verificar la reducción antes de resolver blobs."
        )

    # Espiar BlobStore.get para contar llamadas.
    get_calls = []
    original_get = BlobStore.get

    def spy_get(self, content_hash):
        get_calls.append(content_hash)
        return original_get(self, content_hash)

    monkeypatch.setattr(BlobStore, "get", spy_get)

    # Pedir solo 3 eventos de los 10 con $blob.
    index = LedgerIndex(ledger_path)
    small = index.query(limit=3, include_payloads=True)
    assert len(small) == 3, f"limit=3 debe devolver 3, got {len(small)}"

    # BlobStore.get debe haberse llamado exactamente 3 veces (una por
    # evento devuelto), no 10 (no por evento truncado).
    assert len(get_calls) == 3, (
        f"BlobStore.get debe llamarse 3 veces (una por evento devuelto), "
        f"got {len(get_calls)} — el cap se aplica DESPUÉS de resolver blobs"
    )


def test_query_include_payloads_false_skips_blob_resolution(tmp_path, monkeypatch):
    """Con ``include_payloads=False`` no se resuelven blobs en absoluto
    (``BlobStore.get`` no es llamado) — el payload se deja como ``$blob``
    o se sustituye por un marcador ligero.

    Anti-teatro: una impl que resuelve blobs aún con include_payloads=False
    llama a BlobStore.get y el contador > 0.
    """
    ledger_path = str(tmp_path / "ledger.log")
    blob_dir = str(tmp_path / "blobs")
    os.makedirs(blob_dir, exist_ok=True)

    writer = LedgerWriter(ledger_path)
    large_payload = {"path": "/big", "action": "create", "content": "z" * 5000}
    for i in range(5):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={**large_payload, "idx": i},
        ))

    # Verificar que hay $blob refs leyendo crudo.
    with open(ledger_path) as f:
        raw_lines = [json.loads(ln) for ln in f if ln.strip()]
    has_blob = any(
        isinstance(e["event"].get("payload"), dict) and "$blob" in e["event"]["payload"]
        for e in raw_lines
    )
    if not has_blob:
        pytest.skip("Writer no externalizó a $blob; no aplicable.")

    get_calls = []
    original_get = BlobStore.get

    def spy_get(self, content_hash):
        get_calls.append(content_hash)
        return original_get(self, content_hash)

    monkeypatch.setattr(BlobStore, "get", spy_get)

    index = LedgerIndex(ledger_path)
    slim = index.query(include_payloads=False)
    assert len(slim) == 5
    assert len(get_calls) == 0, (
        f"include_payloads=False no debe resolver blobs (BlobStore.get "
        f"llamado {len(get_calls)} veces)"
    )
    # Los payloads deben conservar el marcador $blob (ligero).
    for entry in slim:
        payload = entry["event"]["payload"]
        assert "$blob" in payload, (
            f"include_payloads=False debe conservar $blob ref, got {payload}"
        )


# ---------------------------------------------------------------------------
# 6. Resource causadb://events respeta el cap
# ---------------------------------------------------------------------------

def test_events_resource_respects_cap(tmp_path):
    """El resource ``causadb://events`` no devuelve la lista completa
    cuando el ledger excede el cap.

    Anti-teatro: un resource que ignora el cap devuelve n eventos y
    falla la aserción.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    n = DEFAULT_QUERY_LIMIT + 100
    for i in range(n):
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="ctx",
            source="test",
            payload={"path": f"/f{i}", "action": "create"},
        ))

    server = create_server(config_ledger_path=ledger_path)

    async def _read():
        contents = await server.read_resource("causadb://events")
        return contents[0].content
    text = anyio.run(_read)
    data = json.loads(text)
    if isinstance(data, dict):
        # C1 (byte cap): 1100 eventos de ~550B ≈ 600KB > 300KB → el cap de
        # bytes se dispara y el resource devuelve marcador truncado.
        assert data["truncated"] is True, "resource truncado → marcador"
        events = data["events"]
    else:
        events = data
    assert len(events) <= DEFAULT_QUERY_LIMIT, (
        f"causadb://events debe respetar el cap (≤ {DEFAULT_QUERY_LIMIT}), "
        f"got {len(events)} (ledger tiene {n})"
    )
    assert len(events) < n, (
        f"no debe devolver toda la lista (n={n}), got {len(events)}"
    )


# ---------------------------------------------------------------------------
# 7. Compatibilidad: include_payloads default True no rompe tests existentes
# ---------------------------------------------------------------------------

def test_query_default_include_payloads_true(tmp_path):
    """El default de ``include_payloads`` es ``True`` (no rompe la firma
    actual ni los tests existentes que esperan payloads completos).

    Anti-teatro: cambiar el default a False rompería los tests que
    inspeccionan payload.path.
    """
    ledger_path = str(tmp_path / "ledger.log")
    writer = LedgerWriter(ledger_path)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED,
        ctx_id="ctx",
        source="test",
        payload={"path": "/foo", "action": "create"},
    ))

    index = LedgerIndex(ledger_path)
    results = index.query()  # default
    assert len(results) == 1
    assert results[0]["event"]["payload"].get("path") == "/foo", (
        "default include_payloads=True debe preservar payload.path"
    )
