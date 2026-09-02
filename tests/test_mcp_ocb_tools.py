"""F1 (M2) — Tests failing-first (Art. III) para las tools MCP OCB.

Plan: BIT-CHR.37; ver docs/design_index.md
- `causadb_ocb_status` — contexto de sesión del OCB (session_type, summary,
  preloaded_partitions, all_partition_ids, partition_metadata opcional,
  total_partitions) con cap anti-gigantismo (BIT-CHR.35 P3) a 50 metadata.
- `causadb_ocb_load_partition` — detalle de una partición con resolución de
  refs `$blob` a pedido (kwarg `resolve_blobs`, agregado en M1) + cap a
  1000 eventos.
- Renderer markdown de revive: secciones "## Resumen de entrada" y
  "## OCB — memoria granular" + punteros `causadb_ocb_*` en
  "## Para profundizar" (Gap 2 + Gap 4).

Thin wrappers (Art. II): las tools delegan en `OCB.for_ledger()` →
`load_session_context()` / `load_context(include_metadata=)` /
`load_partition_by_id(resolve_blobs=)` — 0 lógica duplicada.

Fall-Closed (Art. VIII): cualquier error → `raise ValueError`.

Anti-teatro (Art. IX): los tests *_skipped / *_no_* incluyen mutación
discriminatoria explícita — fallan si el renderer se mutea a
ignorar/siempre-renderear el dato.
"""
import json
import os
from types import MappingProxyType

import pytest
import anyio

from causadb._ocb_manager import OCB
from causadb._blob_store import BlobStore
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb.mcp import _tools
from causadb.mcp.server import create_server
from causadb.cli._cmd_revive import (
    _generate_revive_markdown,
    _generate_drill_down_instructions,
)
from tests.helpers._mcp_call import _call_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ocb_workspace(tmp_path):
    """Crea (ledger_path, ocb_dir). El archivo del ledger puede no existir —
    el OCB vive en ``<dirname(ledger)>/ocb`` (mismo layout que for_ledger).
    """
    ledger_path = str(tmp_path / "ledger.log")
    ocb_dir = str(tmp_path / "ocb")
    os.makedirs(ocb_dir, exist_ok=True)
    return ledger_path, ocb_dir


def _event(payload=None, event_type=EventType.FILE_MODIFIED, ctx_id="ctx",
           source="opencode:agent", event_id=None, timestamp=None):
    kwargs = {
        "event_type": event_type,
        "ctx_id": ctx_id,
        "source": source,
    }
    if payload is not None:
        kwargs["payload"] = MappingProxyType(payload)
    if event_id is not None:
        kwargs["event_id"] = event_id
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return CanonicalEvent(**kwargs)


def _write_partition(ocb_dir, ns: int, events):
    """Escribe una partición ``OCB_PARTITION_<ns>.log`` (nombre time_ns →
    sort lexicográfico = cronológico). Retorna el nombre del archivo."""
    fname = f"OCB_PARTITION_{ns}.log"
    with open(os.path.join(ocb_dir, fname), "w") as f:
        for ev in events:
            f.write(json.dumps(ev.to_dict(), sort_keys=True) + "\n")
    return fname


def _text(content_blocks):
    return "".join(getattr(b, "text", str(b)) for b in content_blocks)


def _revive_data(tmp_path, resume):
    return {"ledger_path": str(tmp_path / "ledger.log"), "resume": resume}


# ===========================================================================
# 1. causadb_ocb_status — contexto de sesión + overview de particiones
# ===========================================================================

def test_mcp_tools_ocb_status(tmp_path):
    """OCB con 1 partición → session_type != first_run + preloaded no vacío
    + all_partition_ids no vacío + total_partitions >= 1."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    ocb = OCB("actor", ocb_dir, threshold_events=1)
    ocb.append(_event(event_id="evt-1"))
    ocb.append(_event(event_id="evt-2"))  # rota → 1 partición

    result = json.loads(_tools.causadb_ocb_status(ledger_path))

    assert result["session_type"] != "first_run"
    assert result["preloaded_partitions"], "preloaded_partitions no vacío"
    assert result["all_partition_ids"], "all_partition_ids no vacío"
    assert result["total_partitions"] >= 1
    assert "partition_metadata" in result, "default include_metadata=True"


def test_mcp_tools_ocb_status_with_metadata(tmp_path):
    """3 particiones con eventos conocidos → partition_metadata con 3 dicts
    (id, first_timestamp, last_timestamp, event_count, session_ids, sources,
    event_types correctos)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    ns0 = 1_700_000_000_000_000_000
    _write_partition(ocb_dir, ns0, [
        _event(event_type=EventType.FILE_MODIFIED, ctx_id="sess-A",
               source="opencode:agent", event_id="e1",
               timestamp="2026-01-01T10:00:00Z"),
    ])
    _write_partition(ocb_dir, ns0 + 1, [
        _event(event_type=EventType.COMMAND_RUN, ctx_id="sess-A",
               source="opencode:agent", event_id="e2",
               timestamp="2026-01-01T11:00:00Z"),
    ])
    _write_partition(ocb_dir, ns0 + 2, [
        _event(event_type=EventType.LLM_INVOKED, ctx_id="sess-B",
               source="harvester:gemini", event_id="e3",
               timestamp="2026-01-01T12:00:00Z"),
    ])

    result = json.loads(_tools.causadb_ocb_status(ledger_path))

    metadata = result["partition_metadata"]
    assert len(metadata) == 3, f"esperaba 3 dicts de metadata, got {len(metadata)}"
    assert len(result["all_partition_ids"]) == 3

    by_id = {m["id"]: m for m in metadata}
    p1 = by_id[f"OCB_PARTITION_{ns0}.log"]
    p2 = by_id[f"OCB_PARTITION_{ns0 + 1}.log"]
    p3 = by_id[f"OCB_PARTITION_{ns0 + 2}.log"]

    # Partición 1: FILE_MODIFIED / sess-A / 10:00
    assert p1["event_count"] == 1
    assert p1["first_timestamp"] == "2026-01-01T10:00:00Z"
    assert p1["last_timestamp"] == "2026-01-01T10:00:00Z"
    # session_ids/sources son sets → default=str los serializa como repr
    # (plan: "serializables con default=str"); verificamos membresía.
    assert "sess-A" in p1["session_ids"]
    assert "opencode:agent" in p1["sources"]
    assert p1["event_types"] == {"FILE_MODIFIED": 1}

    # Partición 2: COMMAND_RUN / sess-A / 11:00
    assert p2["event_count"] == 1
    assert p2["first_timestamp"] == "2026-01-01T11:00:00Z"
    assert p2["last_timestamp"] == "2026-01-01T11:00:00Z"
    assert p2["event_types"] == {"COMMAND_RUN": 1}
    assert "sess-A" in p2["session_ids"]

    # Partición 3: LLM_INVOKED / sess-B / 12:00
    assert p3["event_count"] == 1
    assert p3["first_timestamp"] == "2026-01-01T12:00:00Z"
    assert p3["last_timestamp"] == "2026-01-01T12:00:00Z"
    assert p3["event_types"] == {"LLM_INVOKED": 1}
    assert "sess-B" in p3["session_ids"]
    assert "harvester:gemini" in p3["sources"]


def test_mcp_tools_ocb_status_metadata_truncated(tmp_path):
    """100 particiones → partition_metadata capped a 50 + total_partitions 100
    + truncated True (BIT-CHR.35 P3). all_partition_ids NO se trunca."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    ns0 = 1_700_000_000_000_000_000
    for i in range(100):
        _write_partition(ocb_dir, ns0 + i, [_event(event_id=f"evt-{i}")])

    result = json.loads(_tools.causadb_ocb_status(ledger_path))

    assert len(result["partition_metadata"]) == 50, (
        f"metadata debe capsearse a 50, got {len(result['partition_metadata'])}"
    )
    assert result["total_partitions"] == 100
    assert result["truncated"] is True
    assert len(result["all_partition_ids"]) == 100, (
        "all_partition_ids es la lista completa (no se trunca)"
    )


def test_mcp_tools_ocb_status_no_metadata(tmp_path):
    """include_metadata=False → NO contiene key partition_metadata (solo
    all_partition_ids). Anti-teatro: si la impl siempre la incluye, falla."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    ocb = OCB("actor", ocb_dir, threshold_events=1)
    ocb.append(_event(event_id="evt-1"))
    ocb.append(_event(event_id="evt-2"))  # rota → 1 partición

    result = json.loads(
        _tools.causadb_ocb_status(ledger_path, include_metadata=False)
    )

    assert "partition_metadata" not in result, (
        "include_metadata=False no debe incluir partition_metadata"
    )
    assert "truncated" not in result
    assert result["all_partition_ids"], "all_partition_ids sigue presente"


def test_mcp_tools_ocb_status_empty(tmp_path):
    """OCB vacío → session_type first_run + preloaded [] + all_partition_ids
    [] (no crashea)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)

    result = json.loads(_tools.causadb_ocb_status(ledger_path))

    assert result["session_type"] == "first_run"
    assert result["preloaded_partitions"] == []
    assert result["all_partition_ids"] == []
    assert result["total_partitions"] == 0


def test_mcp_tools_ocb_status_fall_closed_on_error(tmp_path):
    """Fall-Closed (Art. VIII): OCB_SUMMARY.json inválido → ValueError (la
    tool no devuelve JSON parcial ni crashea silenciosamente)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    with open(os.path.join(ocb_dir, "OCB_SUMMARY.json"), "w") as f:
        f.write("{ not valid json ")

    with pytest.raises(ValueError):
        _tools.causadb_ocb_status(ledger_path)


# ===========================================================================
# 2. causadb_ocb_load_partition — detalle granular de una partición
# ===========================================================================

def test_mcp_tools_ocb_load_partition_with_blobs(tmp_path):
    """Partición con líneas $blob + resolve_blobs=True → payloads resueltos
    contra el BlobStore real del fixture (Art. IX — blob real en disco)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", ocb_dir, blob_store=store, blob_store_threshold=1024,
              threshold_events=1)
    big_payload = {"content": "detalle granular " + "x" * 3000}
    ocb.append(_event(payload=big_payload, event_id="blob-1"))
    ocb.append(_event(payload={"small": 1}, event_id="small-1"))  # rota

    partitions = [f for f in os.listdir(ocb_dir) if f.startswith("OCB_PARTITION_")]
    assert len(partitions) == 1

    result = json.loads(_tools.causadb_ocb_load_partition(ledger_path, partitions[0]))

    assert isinstance(result, list), f"lista de eventos dicts, got {type(result)}"
    assert len(result) == 1
    assert result[0]["event_id"] == "blob-1"
    assert result[0]["payload"] == big_payload, (
        "resolve_blobs=True debe resolver el $blob contra el BlobStore"
    )


def test_mcp_tools_ocb_load_partition_metadata_only(tmp_path):
    """resolve_blobs=False → payloads como {resolved: False, $blob: hash}
    (no intenta leer del BlobStore — metadata only)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    store = BlobStore(base_path=str(tmp_path / "blobs"))
    ocb = OCB("actor", ocb_dir, blob_store=store, blob_store_threshold=1024,
              threshold_events=1)
    big_payload = {"content": "detalle granular " + "x" * 3000}
    ocb.append(_event(payload=big_payload, event_id="blob-1"))
    ocb.append(_event(payload={"small": 1}, event_id="small-1"))  # rota

    partitions = [f for f in os.listdir(ocb_dir) if f.startswith("OCB_PARTITION_")]

    result = json.loads(_tools.causadb_ocb_load_partition(
        ledger_path, partitions[0], resolve_blobs=False
    ))

    assert isinstance(result, list)
    assert len(result) == 1
    payload = result[0]["payload"]
    assert payload.get("resolved") is False, (
        "resolve_blobs=False debe marcar resolved: False (no resuelve)"
    )
    assert "$blob" in payload


def test_mcp_tools_ocb_load_partition_missing(tmp_path):
    """partition_id inexistente → [] (no crashea)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)

    result = json.loads(_tools.causadb_ocb_load_partition(
        ledger_path, "OCB_PARTITION_does_not_exist.log"
    ))

    assert result == []


def test_mcp_tools_ocb_load_partition_cap(tmp_path):
    """Partición con >1000 eventos → lista truncada a 1000 +
    {truncated: True, count: N} (BIT-CHR.35 P3)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    fname = _write_partition(
        ocb_dir, 1_700_000_000_000_000_000,
        [_event(event_id=f"evt-{i}") for i in range(1005)],
    )

    result = json.loads(_tools.causadb_ocb_load_partition(ledger_path, fname))

    assert isinstance(result, dict), "cap activado → dict con events/count"
    assert result["truncated"] is True
    assert result["count"] == 1005
    assert len(result["events"]) == 1000


def test_mcp_tools_ocb_load_partition_fall_closed_on_error():
    """Fall-Closed (Art. VIII): ledger_path vacío → ValueError (no devuelve
    JSON vacío silencioso)."""
    with pytest.raises(ValueError):
        _tools.causadb_ocb_load_partition("", "OCB_PARTITION_x.log")


# ===========================================================================
# 3. Renderer markdown de revive — Gap 2 (entry_summary + OCB) y Gap 4
# ===========================================================================

def test_revive_markdown_renders_entry_summary(tmp_path):
    """entry_summary no None → markdown contiene "## Resumen de entrada" +
    contenido (tool, session_id, turn_count, summary_lines).

    Mutación discriminatoria: si el renderer se mutea a ignorar
    entry_summary, esta sección no aparece y el test FALLA."""
    resume = {
        "session_type": "abrupt_close",
        "entry_summary": {
            "tool": "gemini",
            "session_id": "session-abc",
            "turn_count": 5,
            "summary_lines": ["user: test prompt...", "assistant: test response..."],
        },
        "preloaded_partitions": [],
    }
    md = _generate_revive_markdown(_revive_data(tmp_path, resume))

    assert "## Resumen de entrada" in md
    assert "gemini" in md
    assert "session-abc" in md
    assert "5" in md
    assert "test prompt" in md


def test_revive_markdown_entry_summary_none_skipped(tmp_path):
    """entry_summary None → markdown NO contiene la sección.

    Mutación discriminatoria: si el renderer se mutea a siempre renderizar,
    el header aparece y el test FALLA."""
    resume = {
        "session_type": "first_run",
        "entry_summary": None,
        "preloaded_partitions": [],
    }
    md = _generate_revive_markdown(_revive_data(tmp_path, resume))

    assert "## Resumen de entrada" not in md, (
        "entry_summary None no debe renderizar la sección"
    )


def test_revive_markdown_renders_preloaded_partitions(tmp_path):
    """preloaded_partitions no vacío (forma real: lista de IDs, ver
    _cmd_resume.generate_resume) → markdown contiene "## OCB — memoria
    granular" + el ID."""
    resume = {
        "session_type": "abrupt_close",
        "entry_summary": None,
        "preloaded_partitions": ["OCB_PARTITION_1700000000000000000.log"],
    }
    md = _generate_revive_markdown(_revive_data(tmp_path, resume))

    assert "## OCB — memoria granular" in md
    assert "OCB_PARTITION_1700000000000000000.log" in md


def test_revive_markdown_ocb_empty_warning(tmp_path):
    """session_type != first_run + preloaded_partitions [] → línea de warning
    con `causadb ocb rebuild` (Gap 2 — el agente aprende a retroalimentar)."""
    resume = {
        "session_type": "abrupt_close",
        "entry_summary": None,
        "preloaded_partitions": [],
    }
    md = _generate_revive_markdown(_revive_data(tmp_path, resume))

    assert "OCB vacío — correr `causadb ocb rebuild" in md, (
        "debe avisar que el OCB está vacío y sugerir rebuild"
    )


def test_revive_drill_down_includes_ocb_pointer():
    """preloaded_partitions no vacío → "Para profundizar" (drill-down)
    contiene los punteros causadb_ocb_status / causadb_ocb_load_partition +
    gloss "OCB = Operational Context Buffer" (Gap 4)."""
    resume = {"preloaded_partitions": ["OCB_PARTITION_x.log"]}
    lines = _generate_drill_down_instructions(resume, observations=[])

    joined = "\n".join(lines)
    assert "causadb_ocb_status" in joined
    assert "causadb_ocb_load_partition" in joined
    assert "OCB = Operational Context Buffer" in joined


def test_revive_drill_down_no_ocb_pointer_when_empty():
    """preloaded_partitions [] → NO contiene las líneas OCB.

    Mutación discriminatoria: si el renderer se mutea a siempre emitirlas,
    aparecen y el test FALLA (anti-teatro Art. IX)."""
    resume = {"preloaded_partitions": []}
    lines = _generate_drill_down_instructions(resume, observations=[])


    joined = "\n".join(lines)
    assert "causadb_ocb_status" in joined, (
        "causadb_ocb_status debe aparecer siempre"
    )
    assert "causadb_ocb_load_partition" not in joined, (
        "preloaded vacío → no debe emitir el puntero causadb_ocb_load_partition"
    )


def test_revive_drill_down_ocb_status_always_with_preloaded():
    resume = {"preloaded_partitions": ["OCB_PARTITION_x.log"]}
    lines = _generate_drill_down_instructions(resume, observations=[])
    joined = "\n".join(lines)
    assert "causadb_ocb_status" in joined
    assert "causadb_ocb_load_partition" in joined


def test_mcp_tools_ocb_status_total_partitions_no_active(tmp_path):
    """OCB con 1 partición + OCB_ACTIVE.log con eventos (sesión viva)
    → causadb_ocb_status(ledger_path) → result["total_partitions"] == 2 (1 original + 1 rotada)."""
    ledger_path, ocb_dir = _make_ocb_workspace(tmp_path)
    # Escribir 1 partición
    _write_partition(ocb_dir, 1_700_000_000_000_000_000, [_event(event_id="evt-1")])
    # Escribir OCB_ACTIVE.log con eventos (sesión viva)
    active_path = os.path.join(ocb_dir, "OCB_ACTIVE.log")
    with open(active_path, "w") as f:
        f.write('{"event_type": "FILE_MODIFIED", "ctx_id": "active"}\n')

    result = json.loads(_tools.causadb_ocb_status(ledger_path))

    assert result["total_partitions"] == 2, (
        f"total_partitions debe contar las particiones incluyendo la rotación del ACTIVE, got {result['total_partitions']}"
    )



class TestReviveOCB:
    """R.4.1 — Tests para el renderizado del OCB en revive."""

    def test_revive_ocb_table_with_metadata(self, tmp_path):
        resume = {
            "session_type": "normal_close",
            "total_partitions": 2,
            "preloaded_partitions": ["OCB_PARTITION_a.log", "OCB_PARTITION_b.log"],
            "preloaded_metadata": [
                {"id": "OCB_PARTITION_a.log", "event_count": 200, "first_timestamp": "2026-08-08T14:01:23.000000Z", "last_timestamp": "2026-08-08T18:22:45.000000Z"},
                {"id": "OCB_PARTITION_b.log", "event_count": 150, "first_timestamp": "2026-08-08T18:23:01.000000Z", "last_timestamp": "2026-08-08T21:05:11.000000Z"},
            ]
        }
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))

        assert "## OCB — memoria granular" in md
        assert "- **Particiones totales:** 2" in md
        assert "| # | Partición | Eventos | Rango |" in md
        assert "| 1 | `OCB_PARTITION_a.log` | 200 | 2026-08-08T14:01Z → 2026-08-08T18:22Z |" in md
        assert "| 2 | `OCB_PARTITION_b.log` | 150 | 2026-08-08T18:23Z → 2026-08-08T21:05Z |" in md
        assert "⚠️ OCB vacío" not in md

    def test_revive_ocb_first_run_message(self, tmp_path):
        resume = {
            "session_type": "first_run",
            "preloaded_partitions": [],
            "preloaded_metadata": [],
            "total_partitions": 0
        }
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))

        assert "Primera sesión: el OCB se poblará automáticamente con esta sesión." in md
        assert "⚠️ OCB vacío" not in md
        assert "| # | Partición |" not in md

    def test_revive_ocb_no_metadata_fallback_ids(self, tmp_path):
        resume = {
            "session_type": "normal_close",
            "preloaded_partitions": ["OCB_PARTITION_1700000000000000000.log"],
            "total_partitions": 1
            # sin preloaded_metadata (backward-compat)
        }
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))

        assert "- `OCB_PARTITION_1700000000000000000.log`" in md
        assert "| # | Partición |" not in md

    def test_revive_ocb_table_empty_partition_does_not_crash(self, tmp_path):
        resume = {
            "session_type": "normal_close",
            "total_partitions": 1,
            "preloaded_partitions": ["OCB_PARTITION_empty.log"],
            "preloaded_metadata": [
                {"id": "OCB_PARTITION_empty.log", "event_count": 0, "first_timestamp": "", "last_timestamp": ""},
            ]
        }
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))
        assert "| 1 | `OCB_PARTITION_empty.log` | 0 | ? |" in md

    def test_revive_includes_reconstruction_order(self, tmp_path):
        resume = {"session_type": "normal_close", "preloaded_partitions": []}
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))
        
        assert "## Orden de reconstrucción" in md
        assert "1. **revive**" in md
        assert "2. **OCB**" in md
        assert "3. **causadb_query**" in md
        assert "4. **causadb_replay**" in md

    def test_revive_reconstruction_order_always_present(self, tmp_path):
        resume = {
            "session_type": "first_run",
            "preloaded_partitions": [],
            "preloaded_metadata": [],
            "total_partitions": 0
        }
        md = _generate_revive_markdown(_revive_data(tmp_path, resume))
        
        assert "## Orden de reconstrucción" in md


