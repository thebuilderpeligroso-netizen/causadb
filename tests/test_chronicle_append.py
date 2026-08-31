"""Tests for `causadb chronicle append-md` + MCP tool `chronicle_append`.

Sedimentación de BIT-entries al CAUSADB_CHRONICLE.md con template curado
(reemplaza el edit manual del agente). El append al ledger existente
(`chronicle append`) NO se toca — el ledger es la columna vertebral; la
GOV `bit_id` + el `event_id` opcional en Referencias alinean ledger ↔ .md.

TDD RED → GREEN → anti-teatro (Art. IX): los tests validan contenido exacto
del .md, idempotencia real (dos llamadas), regex de prefijo, parse de ambos
formatos y schema validation real de `validate_event_schema`.
"""
import json
import os
import re

import pytest

from causadb._chronicle_append import render_entry, append_entry
from causadb._chronicle_migrate import parse_chronicle_md
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._schema_validator import validate_event_schema

UUID = "de149bd8-328e-4914-bfcc-927ebecf9820"


def _render(**overrides):
    """render_entry con defaults del template curado."""
    kwargs = dict(
        bit_id="BIT-CHR.999",
        title="Test title",
        date="2026-08-17",
        author="Maker",
        nature="FIX CERRADO",
        summary="summary",
        files=["a.py"],
        body="# Contexto\n\ncuerpo",
    )
    kwargs.update(overrides)
    return render_entry(**kwargs)


# ---------------------------------------------------------------------------
# 1. render_entry — template curado
# ---------------------------------------------------------------------------

def test_render_entry_matches_template():
    """render_entry produce el template exacto: header, Fecha/Autor/Naturaleza,
    body y separador `---` final.

    Anti-teatro: un template que omita campos, cambie el orden o no cierre
    con `---` falla la regex.
    """
    rendered = _render()
    pattern = re.compile(
        r"^## BIT-CHR\.999 — Test title\n\n"
        r"\*\*Fecha:\*\* 2026-08-17\n"
        r"\*\*Autor:\*\* Maker\n"
        r"\*\*Naturaleza:\*\* FIX CERRADO\n\n"
        r"# Contexto\n\ncuerpo\n"
        r"\n---\n$",
        re.MULTILINE,
    )
    assert pattern.search(rendered), f"template mismatch:\n{rendered!r}"


def test_render_entry_with_event_id_referencias():
    """Con event_id, el output cita `**Referencias:** event_id: <uuid>` en el
    formato que `_PROSE_EVENT_ID_RE` captura (alineación ledger ↔ .md)."""
    rendered = _render(event_id=UUID)
    assert f"**Referencias:** event_id: {UUID}" in rendered


# ---------------------------------------------------------------------------
# 2. append_entry — escritura al .md
# ---------------------------------------------------------------------------

def test_append_entry_writes_to_md(tmp_path):
    """append_entry crea el archivo si no existe y el contenido termina con
    la entrada renderizada."""
    ledger = str(tmp_path / "ledger.log")
    md = str(tmp_path / "CHRONICLE.md")
    result = append_entry(
        ledger, chronicle_path=md,
        bit_id="BIT-CHR.999", title="Test title", date="2026-08-17",
        author="Maker", nature="FIX CERRADO", body="# Contexto\n\ncuerpo",
    )
    assert result["status"] == "appended"
    assert os.path.exists(md), "el .md debe crearse si no existe"
    content = open(md, encoding="utf-8").read()
    expected = render_entry(
        "BIT-CHR.999", "Test title", "2026-08-17", "Maker", "FIX CERRADO",
        None, None, "# Contexto\n\ncuerpo",
    )
    assert content == expected, "el .md debe contener exactamente la entrada renderizada"


def test_append_entry_idempotent(tmp_path):
    """Segunda llamada con el mismo bit_id → {"status": "already_exists"}
    (NO duplica, NO error crasheante)."""
    ledger = str(tmp_path / "ledger.log")
    md = str(tmp_path / "CHRONICLE.md")
    kwargs = dict(
        bit_id="BIT-CHR.999", title="Test title", date="2026-08-17",
        author="Maker", nature="FIX CERRADO", body="body",
    )
    first = append_entry(ledger, chronicle_path=md, **kwargs)
    second = append_entry(ledger, chronicle_path=md, **kwargs)
    assert first["status"] == "appended"
    assert second["status"] == "already_exists"
    content = open(md, encoding="utf-8").read()
    assert content.count("## BIT-CHR.999") == 1, "el BIT no debe duplicarse"


def test_append_entry_prefix_collision(tmp_path):
    """`## BIT-1` presente NO bloquea `BIT-10` (regex de prefijo exacto);
    `BIT-1` con `## BIT-105` presente → already_exists solo si el id EXACTO
    existe."""
    ledger = str(tmp_path / "ledger.log")
    md_path = tmp_path / "CHRONICLE.md"
    md = str(md_path)
    md_path.write_text("## BIT-1 — Old\n\n**Fecha:** 2026-08-01\n")

    # BIT-10 no es falso positivo de BIT-1
    res = append_entry(
        ledger, chronicle_path=md, bit_id="BIT-10", title="T10",
        date="2026-08-17", author="A", nature="N", body="b",
    )
    assert res["status"] == "appended", f"BIT-10 no debe colisionar con BIT-1: {res}"

    # BIT-1 exacto → already_exists
    res2 = append_entry(
        ledger, chronicle_path=md, bit_id="BIT-1", title="T1",
        date="2026-08-17", author="A", nature="N", body="b",
    )
    assert res2["status"] == "already_exists"

    # BIT-105 presente, BIT-1 NO existe exacto → append funciona
    md_path.write_text("## BIT-105 — X\n\n**Fecha:** 2026-08-01\n")
    res3 = append_entry(
        ledger, chronicle_path=md, bit_id="BIT-1", title="T1",
        date="2026-08-17", author="A", nature="N", body="b",
    )
    assert res3["status"] == "appended", f"BIT-1 no existe exacto, debe appendear: {res3}"


def test_append_entry_fail_closed_no_body(tmp_path):
    """body vacío/None → ValueError (exit path). bit_id/title/date/author
    requeridos también."""
    ledger = str(tmp_path / "ledger.log")
    md = str(tmp_path / "CHRONICLE.md")
    base = dict(bit_id="BIT-1", title="T", date="2026-08-17", author="A",
                nature="N", body="b")

    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "body": None})
    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "body": ""})
    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "bit_id": None})
    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "title": None})
    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "date": None})
    with pytest.raises(ValueError):
        append_entry(ledger, chronicle_path=md, **{**base, "author": None})

    assert not os.path.exists(md), "FAIL-CLOSED: no debe crearse el .md con campos faltantes"


def test_append_entry_fail_closed_no_chronicle(tmp_path):
    """Sin chronicle resoluble (auto-discovery) → ValueError FAIL-CLOSED."""
    ledger = str(tmp_path / "ledger.log")
    with pytest.raises(ValueError, match="FAIL-CLOSED|no encontrado"):
        append_entry(
            ledger, chronicle_path=None,
            bit_id="BIT-1", title="T", date="2026-08-17", author="A",
            nature="N", body="b",
        )


# ---------------------------------------------------------------------------
# 3. parse — ambos formatos (nuevo Autor/Naturaleza + viejo Maker/Checker)
# ---------------------------------------------------------------------------

def test_parse_new_format_autor_naturaleza(tmp_path):
    """Entrada renderizada por render_entry (formato nuevo) → parse_chronicle_md
    la parsea: maker/checker NO vacíos (Autor → maker=checker=autor), date y
    summary correctos (Naturaleza → summary)."""
    md = tmp_path / "CHRONICLE.md"
    md.write_text(_render())
    entries = parse_chronicle_md(str(md))
    assert len(entries) == 1
    e = entries[0]
    assert e["maker"] == "Maker", f"maker vacío/incorrecto: {e['maker']!r}"
    assert e["checker"] == "Maker", f"checker vacío/incorrecto: {e['checker']!r}"
    assert e["date"] == "2026-08-17"
    assert e["summary"] == "FIX CERRADO", f"summary incorrecto: {e['summary']!r}"


def test_parse_new_format_autor_split_plus(tmp_path):
    """`**Autor:** A + B` → maker=A, checker=B (split en '+')."""
    md = tmp_path / "CHRONICLE.md"
    md.write_text(_render(author="Maker (coder) + Checker (opencode)"))
    entries = parse_chronicle_md(str(md))
    assert len(entries) == 1
    e = entries[0]
    assert e["maker"] == "Maker (coder)"
    assert e["checker"] == "Checker (opencode)"


def test_parse_old_format_maker_checker(tmp_path):
    """Entrada formato viejo (**Maker:**/**Checker:**/**Resumen:**) sigue
    parseando (regresión)."""
    md = tmp_path / "CHRONICLE.md"
    md.write_text(
        "## BIT-7.1 — Old\n"
        "**Fecha:** 2026-07-23\n"
        "**Maker:** gemini-cli\n"
        "**Checker:** gemini-cli\n"
        "**Archivos tocados:** a.py, b.py\n"
        "**Resumen:** Implementado X.\n"
    )
    entries = parse_chronicle_md(str(md))
    assert len(entries) == 1
    e = entries[0]
    assert e["maker"] == "gemini-cli"
    assert e["checker"] == "gemini-cli"
    assert e["summary"] == "Implementado X."
    assert e["files_touched"] == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# 4. schema — el dict del formato nuevo pasa validate_event_schema
# ---------------------------------------------------------------------------

def test_migrate_accepts_new_format_entry(tmp_path):
    """El dict parseado del formato nuevo pasa validate_event_schema para
    CHRONICLE_ENTRY (schema exige maker/checker/summary no vacíos)."""
    md = tmp_path / "CHRONICLE.md"
    md.write_text(_render())
    entries = parse_chronicle_md(str(md))
    assert len(entries) == 1
    e = entries[0]
    event = CanonicalEvent(
        event_type=EventType.CHRONICLE_ENTRY,
        ctx_id="chronicle",
        source="causadb:chronicle",
        payload={
            "bit_id": e["bit_id"],
            "title": e["title"],
            "date": e["date"],
            "maker": e["maker"],
            "checker": e["checker"],
            "summary": e["summary"],
            "files_touched": e["files_touched"],
        },
    )
    vr = validate_event_schema(event)
    assert vr.is_valid, f"schema validation failed: {vr.failure_type} — {vr.description}"


# ---------------------------------------------------------------------------
# 5. rebuild del índice post-append (best-effort)
# ---------------------------------------------------------------------------

def test_append_entry_rebuilds_index_best_effort(tmp_path):
    """(a) rebuild falla (ledger inexistente) → append NO crashea.
    (b) ledger existe → rebuild funciona → el BIT aparece en el índice."""
    ledger = str(tmp_path / "ledger.log")
    md = str(tmp_path / "CHRONICLE.md")

    # (a) ledger no existe → rebuild_index lanza FileNotFoundError → best-effort
    result = append_entry(
        ledger, chronicle_path=md, bit_id="BIT-CHR.999", title="T",
        date="2026-08-17", author="A", nature="N", body="b",
    )
    assert result["status"] == "appended"
    assert os.path.exists(md)

    # (b) ledger existe → rebuild_index funciona → BIT en chronicle_index.json
    open(ledger, "a").close()
    result2 = append_entry(
        ledger, chronicle_path=md, bit_id="BIT-CHR.998", title="T2",
        date="2026-08-17", author="A", nature="N", body="b2",
    )
    assert result2["status"] == "appended"
    from causadb._chronicle_index import load_index
    idx = load_index(ledger)
    assert "BIT-CHR.998" in idx["by_bit"], (
        f"rebuild post-append no registró el BIT: {list(idx['by_bit'].keys())}"
    )