"""H8.5 — Tests RED del Agent Activity Report (visibilidad consolidada multi-agente).

Contrato H8.5 (CAUSADB_ROADMAP_HERMES_TRACEABILITY.md):
- ``build_agent_activity_report(state, session_id, from_time, to_time, events)``
  es una función PURA: consume solo la proyección del ReplayEngine ya
  computada (Art. V — nunca lee stores/fuentes), no muta ``state`` y no
  lanza excepciones ante datos raros (``.get()`` con defaults robustos).
- Filtros ``from_time``/``to_time`` (ISO 8601, comparación lexicográfica
  inclusiva) aplican a TODAS las categorías. ``session_id`` aplica SOLO
  donde la proyección expone session (api_attempts → ``hermes_session_id``;
  reasoning_steps/cost_accounted → ``session_id`` si existe). Las categorías
  que no exponen session quedan GLOBALES con nota en ``filter_notes``.
- ``unobserved`` = categorías con count==0 tras filtros (honestidad Art. V:
  sin evidencia se dice "unobserved", no se inventa).
- ``cost_consistency`` solo se computa si el caller provee ``events``
  (raw del ledger); si ``events is None`` → None.

Artículo III (tests RED primero), Artículo IX (aserciones reales y
discriminatorias — nada de ``assert True``).
"""

import pytest

from causadb._agent_activity_report import build_agent_activity_report

CATEGORIES = [
    "files_modified",
    "commands_run",
    "commits_made",
    "api_activity",
    "llm_invocations",
    "reasoning_steps",
    "cost_accounted",
]


def _mk_state(**overrides):
    """Estado mínimo como lo produce ReplayEngine.reconstruct_state()."""
    state = {
        "files_modified": [],
        "commands_run": [],
        "commits_made": [],
        "api_attempts": [],
        "llm_invocations": [],
        "reasoning_steps": [],
        "cost_accounted": [],
        "sessions": [],
    }
    state.update(overrides)
    return state


def test_empty_state_returns_unobserved_all():
    """State vacío → las 7 categorías count==0, todas en unobserved,
    total_events_considered==0 y cost_consistency None (sin events)."""
    report = build_agent_activity_report({})["agent_activity_report"]

    for cat in CATEGORIES:
        assert report[cat]["count"] == 0, f"{cat} debe tener count 0 con state vacío"
    assert sorted(report["unobserved"]) == sorted(CATEGORIES)
    assert report["summary"]["total_events_considered"] == 0
    assert report["cost_consistency"] is None
    assert report["filters"] == {"session_id": None, "from_time": None, "to_time": None}


def test_files_modified_rollup():
    """3 FILE_MODIFIED (2 edit + 1 create) → count==3, by_action
    discriminado, paths listados en orden, NO en unobserved."""
    state = _mk_state(files_modified=[
        {"path": "/a.py", "action": "edit", "timestamp": "2026-08-15T10:00:00Z"},
        {"path": "/b.py", "action": "edit", "timestamp": "2026-08-15T10:01:00Z"},
        {"path": "/c.py", "action": "create", "timestamp": "2026-08-15T10:02:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    fm = report["files_modified"]
    assert fm["count"] == 3
    assert fm["by_action"] == {"edit": 2, "create": 1}
    assert fm["paths"] == ["/a.py", "/b.py", "/c.py"]
    assert "files_modified" not in report["unobserved"]
    assert report["summary"]["total_events_considered"] == 3


def test_commands_run_with_failures():
    """exit_code 0 → ok; 1 y "2" (str) → failure; None → NO failure
    (desconocido no se inventa como fallo, Art. V)."""
    state = _mk_state(commands_run=[
        {"command": "ok", "exit_code": 0, "timestamp": "2026-08-15T10:00:00Z"},
        {"command": "fail", "exit_code": 1, "timestamp": "2026-08-15T10:01:00Z"},
        {"command": "noexit", "exit_code": None, "timestamp": "2026-08-15T10:02:00Z"},
        {"command": "strfail", "exit_code": "2", "timestamp": "2026-08-15T10:03:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    cr = report["commands_run"]
    assert cr["count"] == 4
    assert cr["with_failures"] == 2
    assert cr["commands"] == ["ok", "fail", "noexit", "strfail"]


def test_commits_made_rollup():
    """Commits listados por message; message ausente → fallback commit_hash
    (default robusto, nunca None silencioso)."""
    state = _mk_state(commits_made=[
        {"commit_hash": "abc", "message": "fix bug", "timestamp": "2026-08-15T10:00:00Z"},
        {"commit_hash": "def", "message": None, "timestamp": "2026-08-15T10:01:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    cm = report["commits_made"]
    assert cm["count"] == 2
    assert cm["commits"] == ["fix bug", "def"]
    assert "commits_made" not in report["unobserved"]


def test_api_activity_rollup_with_tokens():
    """1 success (tokens_out=42) + 1 failed (con error) → success==1,
    failed==1, tokens sumados, by_model discriminado."""
    state = _mk_state(api_attempts=[
        {
            "hermes_session_id": "s1", "provider": "ollama", "model": "qwen3.5:4b",
            "mode": "chat", "status": "success", "request_ref": "r1",
            "tokens_in": 10, "tokens_out": 42, "cost_usd": 0.0,
            "timestamp": "2026-08-15T10:00:00Z",
        },
        {
            "hermes_session_id": "s1", "provider": "ollama", "model": "qwen3.5:4b",
            "mode": "chat", "status": "failed", "request_ref": "r2",
            "tokens_in": 5, "tokens_out": 0, "cost_usd": 0.0, "error": "boom",
            "timestamp": "2026-08-15T10:01:00Z",
        },
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    api = report["api_activity"]
    assert api["count"] == 2
    assert api["success"] == 1
    assert api["failed"] == 1
    assert api["tokens_in"] == 15
    assert api["tokens_out"] == 42
    assert api["cost_usd"] == 0.0
    assert api["by_model"] == {"qwen3.5:4b": 2}


def test_llm_invocations_response_tokens_sum():
    """3 LLM_INVOKED (response_tokens 0, 42, 77) → count==3, suma==119,
    by_model discriminado (0 no se cuenta como ausencia)."""
    state = _mk_state(llm_invocations=[
        {"model": "m1", "response_tokens": 0, "timestamp": "2026-08-15T10:00:00Z"},
        {"model": "m1", "response_tokens": 42, "timestamp": "2026-08-15T10:01:00Z"},
        {"model": "m2", "response_tokens": 77, "timestamp": "2026-08-15T10:02:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    llm = report["llm_invocations"]
    assert llm["count"] == 3
    assert llm["response_tokens"] == 119
    assert llm["by_model"] == {"m1": 2, "m2": 1}
    assert "llm_invocations" not in report["unobserved"]


def test_reasoning_steps_by_kind_robust_defaults():
    """La proyección de reasoning_steps NO garantiza campos fijos: kind →
    fallback step_type → "unknown"; count==3 discriminado por by_kind."""
    state = _mk_state(reasoning_steps=[
        {"kind": "analysis", "summary": "s1", "timestamp": "2026-08-15T10:00:00Z"},
        {"step_type": "planning", "timestamp": "2026-08-15T10:01:00Z"},
        {"timestamp": "2026-08-15T10:02:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    rs = report["reasoning_steps"]
    assert rs["count"] == 3
    assert rs["by_kind"] == {"analysis": 1, "planning": 1, "unknown": 1}
    assert "reasoning_steps" not in report["unobserved"]


def test_cost_accounted_rollup():
    """2 COST_ACCOUNTED → tokens/cost sumados, currency conservada."""
    state = _mk_state(cost_accounted=[
        {"model": "m1", "tokens_in": 100, "tokens_out": 50, "cost": 0.01, "currency": "USD", "timestamp": "2026-08-15T10:00:00Z"},
        {"model": "m1", "tokens_in": 200, "tokens_out": 100, "cost": 0.02, "currency": "USD", "timestamp": "2026-08-15T10:01:00Z"},
    ])
    report = build_agent_activity_report(state)["agent_activity_report"]

    ca = report["cost_accounted"]
    assert ca["count"] == 2
    assert ca["tokens_in"] == 300
    assert ca["tokens_out"] == 150
    assert abs(ca["cost"] - 0.03) < 1e-9
    assert ca["currency"] == "USD"
    assert "cost_accounted" not in report["unobserved"]


def test_time_filter_inclusive():
    """from_time/to_time son bounds INCLUSIVOS (lexicográfico ISO): entran
    12:00 y 14:00 exactos; 11:00 y 15:00/16:00 quedan fuera. total_events_
    considered coincide con la suma de las categorías filtradas."""
    state = _mk_state(
        files_modified=[
            {"path": "/early.py", "action": "create", "timestamp": "2026-08-15T11:00:00Z"},
            {"path": "/mid.py", "action": "edit", "timestamp": "2026-08-15T13:00:00Z"},
            {"path": "/late.py", "action": "edit", "timestamp": "2026-08-15T15:00:00Z"},
        ],
        commands_run=[
            {"command": "ls", "exit_code": 0, "timestamp": "2026-08-15T12:00:00Z"},
            {"command": "make", "exit_code": 1, "timestamp": "2026-08-15T14:00:00Z"},
            {"command": "git", "exit_code": 0, "timestamp": "2026-08-15T16:00:00Z"},
        ],
    )
    report = build_agent_activity_report(
        state,
        from_time="2026-08-15T12:00:00Z",
        to_time="2026-08-15T14:00:00Z",
    )["agent_activity_report"]

    assert report["files_modified"]["count"] == 1
    assert report["files_modified"]["paths"] == ["/mid.py"]
    assert report["files_modified"]["by_action"] == {"edit": 1}
    assert report["commands_run"]["count"] == 2  # bounds inclusive: 12:00 y 14:00
    assert report["commands_run"]["with_failures"] == 1
    assert report["summary"]["total_events_considered"] == 3
    assert report["filters"]["from_time"] == "2026-08-15T12:00:00Z"
    assert report["filters"]["to_time"] == "2026-08-15T14:00:00Z"
    assert "files_modified" not in report["unobserved"]
    assert "commands_run" not in report["unobserved"]


def test_session_filter_only_applies_where_exposed():
    """session_id="AAA" filtra SOLO api_attempts (hermes_session_id);
    files_modified (sin session en la proyección) queda GLOBAL (count 1) y
    filter_notes lo declara; no se descarta silenciosamente."""
    state = _mk_state(
        api_attempts=[
            {
                "hermes_session_id": "AAA", "status": "success", "model": "m1",
                "tokens_in": 1, "tokens_out": 2, "timestamp": "2026-08-15T10:00:00Z",
            },
            {
                "hermes_session_id": "BBB", "status": "success", "model": "m1",
                "tokens_in": 3, "tokens_out": 4, "timestamp": "2026-08-15T10:01:00Z",
            },
        ],
        files_modified=[
            {"path": "/x.py", "action": "create", "timestamp": "2026-08-15T10:02:00Z"},
        ],
    )
    report = build_agent_activity_report(state, session_id="AAA")["agent_activity_report"]

    assert report["api_activity"]["count"] == 1
    assert report["api_activity"]["tokens_out"] == 2
    assert report["api_activity"]["tokens_in"] == 1
    # files_modified NO filtra por session: queda global, sin descartar
    assert report["files_modified"]["count"] == 1
    assert "files_modified" not in report["unobserved"]
    assert report["api_activity"]["count"] > 0
    assert "api_activity" not in report["unobserved"]
    # La nota declara explícitamente el no-filtrado de files_modified
    assert any(
        "files_modified" in note and "session" in note
        for note in report["filter_notes"]
    )
    # total = files global (1) + api filtrada (1)
    assert report["summary"]["total_events_considered"] == 2


def test_cost_consistency_when_events_provided():
    """events raw con API_ATTEMPT + COST_ACCOUNTED + LLM_INVOKED de la misma
    sesión consistentes → cost_consistency es un dict (no None) sin
    duplicación ni discrepancia (formato que espera validate_hermes_consistency)."""
    events = [
        {"type": "API_ATTEMPT", "hermes_session_id": "s1", "tokens_out": 100},
        {"type": "COST_ACCOUNTED", "hermes_session_id": "s1", "tokens_out": 100},
        {"type": "LLM_INVOKED", "hermes_session_id": "s1", "response_tokens": 50},
    ]
    report = build_agent_activity_report({}, events=events)["agent_activity_report"]

    cc = report["cost_consistency"]
    assert cc is not None
    assert "s1" in cc
    assert cc["s1"]["api_attempt_tokens_out"] == 100
    assert cc["s1"]["cost_accounted_tokens_out"] == 100
    assert cc["s1"]["llm_invoked_response_tokens"] == 50
    assert cc["s1"]["duplication_detected"] is False
    assert cc["s1"]["discrepancy_detected"] is False


def test_key_missing_in_state_does_not_crash():
    """state sin la key "api_attempts" (solo files_modified) → sin KeyError,
    api_activity count==0 y en unobserved; files_modified intacto."""
    state = {
        "files_modified": [
            {"path": "/a.py", "action": "create", "timestamp": "2026-08-15T10:00:00Z"},
        ]
    }
    report = build_agent_activity_report(state)["agent_activity_report"]

    assert report["api_activity"]["count"] == 0
    assert "api_activity" in report["unobserved"]
    assert report["files_modified"]["count"] == 1
    assert report["summary"]["total_events_considered"] == 1


def test_agents_sources_observed_from_meta_and_entries():
    """agents_sources_observed une state.meta.sources con los entry source
    de la proyección (valores únicos no vacíos)."""
    state = _mk_state(files_modified=[
        {
            "path": "/a.py", "action": "create", "timestamp": "2026-08-15T10:00:00Z",
            "source": "opencode:agent",
        },
    ])
    state["meta"] = {"sources": ["hermes", "opencode:agent"]}
    report = build_agent_activity_report(state)["agent_activity_report"]

    assert sorted(report["summary"]["agents_sources_observed"]) == sorted(
        ["hermes", "opencode:agent"]
    )


def test_agents_sources_observed_unknown_when_none():
    """Sin ningún source observable → ["unknown"] (honestidad Art. V)."""
    report = build_agent_activity_report({})["agent_activity_report"]
    assert report["summary"]["agents_sources_observed"] == ["unknown"]
