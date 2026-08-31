"""P4 — Perf revive (~51s → ~11-12s): skills desde replay_state (A2) +
compute_score sin re-lecturas (C).

Diseño aprobado por auditoría (3 ajustes al plan original):

* **A2** — El renderer de revive consume ``data["skills_precomputed"]``
  (derivado de ``replay_state["skills"]`` en ``_run_revive``); cero
  llamadas a ``load_skills`` en TODO el pipeline. Esto preserva la
  invariante "==1 reconstruct_state en todo el pipeline" que
  ``test_run_revive_passes_state_to_generate_resume`` cuida globalmente.
* **B DESCARTADO** — el loop de recorte queda 1-a-1 intacto.
* **C** — ``compute_score`` NO consume reconstruct_state: acepta
  ``entries`` keyword-only y comparte la lista de entradas crudas
  materializada UNA vez entre churn y waste (antes: 2 lecturas completas,
  ~6.4s en el ledger real).

Tests RED primero (Art. III / IX): T1, T4, T5 y T7 fallan contra el
código actual; T2, T3 y T6 son guardias de regresión que deben seguir
pasando antes y después.
"""

import json
import re
import time
from types import MappingProxyType
from unittest.mock import patch

import pytest

import causadb._score as score_mod
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._init import causadb_init
from causadb._ledger_reader import LedgerReader
from causadb._ledger_writer import LedgerWriter
from causadb._skill_registry import load_skills, register_skill
from causadb.cli._cmd_revive import _run_revive


# Ledger real del proyecto Master (integración de performance, igual que
# test_revive_single_replay.py FASE 2).
LEDGER_REAL = (
    "/home/juliussb/Recupero Linux/Proyectos/Cortex Agents/Master/.causadb/ledger.log"
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _new_ledger(tmp_path):
    result = causadb_init(str(tmp_path / "ws"))
    return result["ledger_path"], LedgerWriter(result["ledger_path"])


def _append(writer, event_type, payload, **kwargs):
    writer.append(CanonicalEvent(
        event_type=event_type,
        ctx_id="test",
        source="causadb:test",
        payload=MappingProxyType(payload),
        **kwargs,
    ))


def _skill_payload(name, stype="file_tree", content="archivo_a.py\narchivo_b.py"):
    """Payload de SKILL_CREATED (mismas keys que register_skill)."""
    return {
        "skill_type": stype,
        "skill_name": name,
        "content": content,
        "token_count": 10,
        "confidence": 0.9,
        "source_session": "s",
    }


def _decision_payload(tag, pad=300):
    return {
        "reasoning": f"{tag} " + "x" * pad,
        "impact": "low",
        "decision_type": "tactical",
        "origin": "agent",
    }


def _skill_table_names(markdown_output):
    """Extraer los nombres de la tabla '## Skills disponibles' en orden.

    Acotada al bloque de esa sección (hasta el próximo heading ``## ``) y
    saltando header/separador — otras secciones (ej: actividad reciente)
    también tienen filas con pipes.
    """
    if "## Skills disponibles" not in markdown_output:
        return []
    section = markdown_output.split("## Skills disponibles", 1)[1]
    stop = re.search(r"^## ", section, re.M)
    if stop:
        section = section[:stop.start()]
    names = []
    for line in section.splitlines():
        m = re.match(r"^\|\s*([^|]+)\|\s*([^|]+)\|", line)
        if not m:
            continue
        name = m.group(2).strip()
        if name == "Nombre" or set(name) <= {"-", " "}:  # header / separador
            continue
        names.append(name)
    return names


def _decisions_section(markdown_output):
    """Extraer solo la sección '## Decisiones de Gobernanza'.

    La sección 'Actividad reciente del agente' también surfacea decisiones
    recientes (independiente del trim): contar visibles ahí falsearía el
    guardia del recorte.
    """
    start = markdown_output.find("## Decisiones de Gobernanza")
    if start == -1:
        return ""
    end = markdown_output.find("## Tools Disponibles", start)
    if end == -1:
        end = len(markdown_output)
    return markdown_output[start:end]


# ── T1 — EL DISCRIMINADOR anti-teatro A2 ─────────────────────────────────────


def test_t1_revive_markdown_never_calls_load_skills(monkeypatch, tmp_path):
    """A2: el pipeline completo de ``_run_revive`` (markdown) NO llama
    ``load_skills`` — los skills salen de ``replay_state["skills"]`` vía
    ``skills_precomputed``. Hoy el renderer llama load_skills ≥1 vez
    (re-play completo extra ~3.4s en el ledger real).

    Anti-teatro: el ledger tiene ≥3 SKILL_CREATED (la sección debe
    renderizarse) + decisiones suficientes para recortar (cap chico), así
    que ``calls == 0`` solo pasa si el renderer consume el dato
    pre-computado Y la tabla sigue presente.
    """
    ledger, writer = _new_ledger(tmp_path)
    # Setup normal_close OCB to see file_tree skills
    ocb_dir = tmp_path / "ws" / "ocb"
    ocb_dir.mkdir(parents=True, exist_ok=True)
    (ocb_dir / "OCB_SUMMARY.json").write_text('{"sedimentada": true}')
    (ocb_dir / "OCB_SESSION_1234567890.log").write_text('{"event_id": "x"}\n')

    for i in range(3):
        _append(writer, EventType.SKILL_CREATED, _skill_payload(f"skill_p4_{i}"))
    for i in range(6):
        _append(
            writer, EventType.GOVERNANCE_DECISION, _decision_payload(f"DEC_P4_{i}")
        )
    monkeypatch.setenv("CAUSADB_MAX_REVIVE_BYTES", "2000")

    import causadb._skill_registry as skill_registry

    calls = {"n": 0}
    real_load_skills = skill_registry.load_skills

    def counting_load_skills(*args, **kwargs):
        calls["n"] += 1
        return real_load_skills(*args, **kwargs)

    monkeypatch.setattr(skill_registry, "load_skills", counting_load_skills)

    exit_code, output = _run_revive(
        ledger, output_format="markdown", max_decisions=10
    )

    assert exit_code == 0, f"revive falló: {output[:500]!r}"
    assert calls["n"] == 0, (
        f"load_skills llamado {calls['n']} veces en el pipeline de revive — "
        f"A2 exige 0 (skills desde replay_state)."
    )
    assert "## Skills disponibles" in output, (
        "la tabla de skills desapareció del markdown — el wiring de "
        "skills_precomputed está roto.\nOutput:\n"
        f"{output[:800]}"
    )
    assert "skill_p4_0" in output, "el skill registrado no aparece en la tabla"


# ── T2 — correctitud del orden (contrato BIT-CHR.103 duplicado) ─────────────


def test_t2_skills_order_matches_load_skills_contract(monkeypatch, tmp_path):
    """La tabla de skills del markdown de ``_run_revive`` debe ser idéntica
    en contenido Y orden a la salida filtrada por session_type
    (orden timestamp DESC, contrato BIT-CHR.103).

    Ledger con timestamps DESCENTES en orden de append: el replay conserva
    el orden del ledger, así que sin el sort la tabla quedaría invertida.
    Usamos normal_close para ver file_tree skills.
    """
    ledger, writer = _new_ledger(tmp_path)
    # Setup normal_close OCB to see file_tree skills
    # ledger is at tmp_path/ws/ledger.log, OCB at tmp_path/ws/ocb
    ocb_dir = tmp_path / "ws" / "ocb"
    ocb_dir.mkdir(parents=True, exist_ok=True)
    (ocb_dir / "OCB_SUMMARY.json").write_text('{"sedimentada": true}')
    (ocb_dir / "OCB_SESSION_1234567890.log").write_text('{"event_id": "x"}\n')

    # Append en orden viejo→nuevo pero con timestamps descendentes:
    # replay_state["skills"] queda [viejo, medio, nuevo]; el orden
    # timestamp-desc correcto es [nuevo, medio, viejo].
    timestamps = ["2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z", "2026-01-05T00:00:00Z"]
    names = ["p4_viejo", "p4_medio", "p4_nuevo"]
    for name, ts in zip(names, timestamps):
        _append(
            writer, EventType.SKILL_CREATED, _skill_payload(name), timestamp=ts
        )

    exit_code, output = _run_revive(
        ledger, output_format="markdown", max_decisions=10
    )

    assert exit_code == 0, f"revive falló: {output[:500]!r}"
    table_names = _skill_table_names(output)
    # load_skills with types=["file_tree"] to match normal_close filtering
    from causadb._skill_registry import load_skills
    expected = [s["skill_name"] for s in load_skills(ledger, types=["file_tree"])]
    assert table_names == expected, (
        f"orden de la tabla {table_names} != load_skills filtered {expected}"
    )
    assert table_names == ["p4_nuevo", "p4_medio", "p4_viejo"], (
        f"orden timestamp-desc incorrecto: {table_names}"
    )


# ── T3 — regresión del recorte (guardia: hoy pasa, debe SEGUIR pasando) ─────


def test_t3_trim_notice_regression(monkeypatch, tmp_path):
    """Cap chico + N decisiones conocidas: el aviso ``+N omitidas`` debe ser
    exactamente inicial − final, solo las finales visibles y en orden
    newest-first (las más viejas se recortan primero)."""
    n = 8
    ledger, writer = _new_ledger(tmp_path)
    for i in range(n):
        _append(
            writer,
            EventType.GOVERNANCE_DECISION,
            _decision_payload(f"TRIM_P4_{i}", pad=300),
        )
    monkeypatch.setenv("CAUSADB_MAX_REVIVE_BYTES", "2500")

    exit_code, output = _run_revive(
        ledger, output_format="markdown", max_decisions=n
    )

    assert exit_code == 0, f"revive falló: {output[:500]!r}"
    match = re.search(r"\+(\d+) decisiones omitidas", output)
    assert match, f"sin aviso de recorte en output:\n{output[-400:]}"
    dropped = int(match.group(1))

    decisions_section = _decisions_section(output)
    visible = [i for i in range(n) if f"TRIM_P4_{i}" in decisions_section]
    assert dropped == n - len(visible), (
        f"aviso dice +{dropped} pero inicial({n}) - final({len(visible)}) "
        f"= {n - len(visible)}"
    )
    # newest-first: se recortan las más VIEJAS (índices bajos); quedan los
    # índices altos [dropped .. n-1].
    assert visible == list(range(dropped, n)), (
        f"visibles {visible} != esperado {list(range(dropped, n))}"
    )


# ── T4 — perf e2e contra el ledger real ──────────────────────────────────────

# Moved to benchmarks/test_revive_perf.py

# ── T5 — C: compute_score(entries=...) cero re-lecturas ─────────────────────


def test_t5_compute_score_with_entries_zero_rereads(monkeypatch, tmp_path):
    """C: ``compute_score(path, entries=pre-leídas)`` NO instancia
    ``LedgerReader`` en el namespace de ``causadb._score`` (ni churn ni
    waste re-leen) Y produce números IDÉNTICOS al camino legacy.

    Anti-teatro: si compute_score ignora ``entries``, el contador dispara
    (re-lecturas) o el TypeError de firma mata el test.
    """
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.LLM_INVOKED, {"model": "gpt-4", "cost": 0.05})
    _append(writer, EventType.FILE_MODIFIED, {"path": "/p4_a.py", "action": "create"})
    _append(writer, EventType.FILE_MODIFIED, {"path": "/p4_a.py", "action": "modify"})

    # Baseline legacy (lee el ledger él mismo).
    legacy = score_mod.compute_score(ledger)

    # Materializar entradas UNA vez (lo que hará _run_revive post-P4).
    entries = list(LedgerReader(ledger).read_all_entries())
    assert len(entries) >= 3

    calls = {"n": 0}

    class CountingReader(LedgerReader):
        def read_all_entries(self, *args, **kwargs):
            calls["n"] += 1
            return super().read_all_entries(*args, **kwargs)

    monkeypatch.setattr(score_mod, "LedgerReader", CountingReader)

    with_entries = score_mod.compute_score(ledger, entries=entries)

    assert calls["n"] == 0, (
        f"compute_score(entries=...) leyó el ledger {calls['n']} veces — "
        f"C exige 0 re-lecturas cuando entries viene."
    )
    assert with_entries == legacy, (
        f"resultado con entries difiere del legacy:\n"
        f"legacy={legacy}\nwith_entries={with_entries}"
    )


def test_t5b_compute_churn_and_waste_accept_entries_directly(monkeypatch, tmp_path):
    """Las tres funciones aceptan ``entries`` keyword-only y con ellas hacen
    CERO lecturas (churn y waste individualmente, no solo vía compute_score)."""
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.LLM_INVOKED, {"model": "gpt-4", "cost": 0.02})
    _append(writer, EventType.FILE_MODIFIED, {"path": "/p4_b.py", "action": "create"})

    legacy_churn = score_mod.compute_churn(ledger)
    legacy_waste = score_mod.compute_waste(ledger)

    entries = list(LedgerReader(ledger).read_all_entries())

    calls = {"n": 0}

    class CountingReader(LedgerReader):
        def read_all_entries(self, *args, **kwargs):
            calls["n"] += 1
            return super().read_all_entries(*args, **kwargs)

    monkeypatch.setattr(score_mod, "LedgerReader", CountingReader)

    churn = score_mod.compute_churn(ledger, entries=entries)
    waste = score_mod.compute_waste(ledger, entries=entries)

    assert calls["n"] == 0, f"re-lecturas con entries: {calls['n']}"
    assert churn == legacy_churn
    assert waste == legacy_waste


# ── T6 — compat firmas posicionales (rest_api.py:774) ────────────────────────


def test_t6_positional_config_still_works(tmp_path):
    """Compat: la llamada posicional estilo ``_rest_api.py:774``
    (``compute_score(path, config)``) sigue funcionando — los nuevos
    parámetros son KEYWORD-ONLY después de config."""
    ledger, writer = _new_ledger(tmp_path)
    _append(writer, EventType.FILE_MODIFIED, {"path": "/p4_c.py", "action": "create"})
    config = CausaDBConfig(ledger_path=ledger)

    # Posicional EXACTO como _rest_api.py:774.
    result = score_mod.compute_score(ledger, config)
    assert isinstance(result, dict), f"compute_score(posicional) rompió: {result!r}"
    assert "overall_score" in result

    churn = score_mod.compute_churn(ledger, config)
    waste = score_mod.compute_waste(ledger, config)
    assert isinstance(churn, dict) and isinstance(waste, dict)


# ── T7 — R4 + skills: blob faltante degrada limpio, sin entrada de skills ────


def test_t7_missing_blob_skills_precomputed_empty_no_skill_degradation(
    tmp_path,
):
    """Blob faltante (patrón test_blob_resolution): ``skills_precomputed ==
    []``, banner presente, sin crash, y ``degraded_detail`` SIN entrada de
    skills (semántica R4: fallo de skills NO entra al banner)."""
    ws = tmp_path / "ws"
    result = causadb_init(str(ws))
    ledger = result["ledger_path"]
    config = CausaDBConfig(ledger_path=ledger, blob_store_enabled=True)
    writer = LedgerWriter(ledger, config=config)
    fake_hash = "f" * 64
    payload = {
        "reasoning": "decisión blobificada P4 " + "v" * 2000,
        "impact": "high",
        "decision_type": "tactical",
        "origin": "agent",
    }
    with patch("causadb._blob_store.BlobStore.put", return_value=fake_hash):
        writer.append(CanonicalEvent(
            event_type=EventType.GOVERNANCE_DECISION,
            ctx_id="test",
            source="causadb:test",
            payload=MappingProxyType(payload),
        ))

    # JSON: skills_precomputed == [] + degraded estructurado sin "skill".
    rc_json, out_json = _run_revive(
        ledger, output_format="json", max_decisions=10
    )
    assert rc_json == 0, f"revive json crash con blob faltante: {out_json[:400]!r}"
    data = json.loads(out_json)
    assert data.get("skills_precomputed") == [], (
        f"skills_precomputed debería ser [] ante replay degradado, got: "
        f"{data.get('skills_precomputed')!r}"
    )
    assert data["degraded"] is True
    # Semántica R4 exacta: degraded_detail solo contiene errores de
    # resolución de $blob — un fallo del bloque de skills NO entra al
    # banner (los mensajes de blob siempre mencionan "blob").
    errors = data.get("degraded_detail", {}).get("errors", [])
    assert errors, "se esperaba al menos un error de blob en degraded_detail"
    for err in errors:
        assert "blob" in err.lower(), (
            f"degraded_detail solo debe contener errores de blobs (R4), "
            f"no fallos de skills ni otros: {err[:200]}"
        )

    # Markdown: banner al tope, sin crash.
    rc_md, out_md = _run_revive(
        ledger, output_format="markdown", max_decisions=10
    )
    assert rc_md == 0, f"revive markdown crash con blob faltante: {out_md[:400]!r}"
    assert "BLOBS FALTANTES" in out_md, f"sin banner R4: {out_md[:400]!r}"
