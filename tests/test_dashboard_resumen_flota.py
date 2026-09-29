"""Dashboard Resumen/Flota + trades (copias de diseño) — TDD RED→GREEN.

Cubre, sobre las COPIAS DE DISEÑO (open-design, las instala el Checker),
comportamiento REAL ejecutado en node (no asserts de mera presencia):

a) ``groupTrades`` agrupa entry/exit por ``payload.trade_id``.
b) ``groupSessions`` agrupa por sesión con conteos de eventos y FILE_MODIFIED.
c) Con lista vacía el render de trades muestra el texto exacto
   "Este ledger no tiene operaciones" + nota con "1000"/"recientes".

RED: estos 3 tests FALLAN contra las copias viejas (trades.js sin
groupTrades con ``window``; app.js sin groupSessions y sin el texto exacto
en el render de trades).
GREEN: pasan tras implementar trades.js + app.js en las copias.

NOTA: estos tests apuntan a las copias de diseño, NO a
``causadb/dashboard`` (producción, prohibido tocarla en este workflow).
"""
import json
import pathlib
import subprocess

DESIGN = pathlib.Path(
    "/home/juliussb/.local/share/open-design/.od/projects/causadb-dashboard-f9bf"
)


def _read(name):
    return (DESIGN / name).read_text(encoding="utf-8")


def _run_node(script):
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node falló: {proc.stderr[:600]}"
    return proc.stdout


def _extract_function(src, name):
    """Extrae ``function <name>(...) {...}`` con balanceo de llaves.

    Falla (RED) si la función no existe: el agrupado no es testeable.
    """
    marker = "function " + name + "("
    start = src.find(marker)
    assert start != -1, f"app.js no define function {name} (agrupado no testeable)"
    brace = src.find("{", start)
    assert brace != -1, f"function {name} sin cuerpo"
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"no se pudo extraer function {name}")


def _trade_events():
    return [
        {
            "event_type": "TRADE_EXECUTED",
            "payload": {
                "trade_id": "t-1",
                "phase": "entry",
                "symbol": "BTC/USDT",
                "enter_tag": "breakout",
                "indicators_entry": {"rsi": 55},
                "indicators_provenance": {"rsi": "closed_candle"},
            },
        },
        {
            "event_type": "TRADE_EXECUTED",
            "payload": {
                "trade_id": "t-1",
                "phase": "exit",
                "symbol": "BTC/USDT",
                "exit_reason": "take_profit",
                "indicators_entry": {"rsi": 55},
                "indicators_provenance": {"rsi": "closed_candle"},
            },
        },
    ]


def test_group_trades_groups_entry_exit_by_trade_id():
    """groupTrades agrupa entry+exit del mismo trade_id en UN grupo."""
    src = _read("trades.js")
    script = (
        src
        + "\nvar __events = "
        + json.dumps(_trade_events())
        + ";\nconsole.log(JSON.stringify(groupTrades(__events)));"
    )
    groups = json.loads(_run_node(script))
    assert isinstance(groups, list) and len(groups) == 1, groups
    g = groups[0]
    assert g["trade_id"] == "t-1"
    assert g["entry"]["payload"]["enter_tag"] == "breakout"
    assert g["exit"]["payload"]["exit_reason"] == "take_profit"
    assert g["entry"]["payload"]["indicators_entry"] == {"rsi": 55}
    assert g["exit"]["payload"]["indicators_provenance"] == {"rsi": "closed_candle"}


def test_groups_by_session_with_event_and_file_counts():
    """groupSessions agrupa por sesión con conteos reales (node)."""
    fn = _extract_function(_read("app.js"), "groupSessions")
    events = [
        {"event_type": "FILE_MODIFIED", "session_id": "s-1",
         "payload": {"path": "/tmp/a.txt"}},
        {"event_type": "TOOL_CALLED", "session_id": "s-1", "payload": {}},
        {"event_type": "FILE_MODIFIED", "session_id": "s-2",
         "payload": {"path": "/tmp/b.txt"}},
        {"event_type": "SESSION_SUMMARY", "session_id": "s-2", "payload": {}},
    ]
    script = (
        fn
        + "\nvar __events = "
        + json.dumps(events)
        + ";\nconsole.log(JSON.stringify(groupSessions(__events)));"
    )
    groups = json.loads(_run_node(script))
    by_id = {g["id"]: g for g in groups}
    assert set(by_id) == {"s-1", "s-2"}, by_id
    assert by_id["s-1"]["eventCount"] == 2, by_id["s-1"]
    assert by_id["s-1"]["filesModified"] == 1, by_id["s-1"]
    assert by_id["s-2"]["eventCount"] == 2, by_id["s-2"]
    assert by_id["s-2"]["filesModified"] == 1, by_id["s-2"]


def test_empty_trades_render_exact_copy():
    """Vacío in → vacío out (node) + texto exacto en el render de trades."""
    src = _read("trades.js")
    out = _run_node(src + "\nconsole.log(JSON.stringify(groupTrades([])));")
    assert json.loads(out) == []
    # El texto exacto vive DENTRO del render de trades (no grep global).
    fn = _extract_function(_read("app.js"), "renderTrades")
    assert "Este ledger no tiene operaciones" in fn, "render sin texto exacto de vacío"
    assert "1000" in fn and "recientes" in fn, "render sin nota de tope 1000"


def test_count_commands_counts_only_command_run():
    """countByType cuenta solo COMMAND_RUN (fixture mixta de 3 eventos)."""
    fn = _extract_function(_read("app.js"), "countByType")
    events = [
        {"event_type": "COMMAND_RUN", "payload": {"command": "ls"}},
        {"event_type": "FILE_MODIFIED", "payload": {"path": "/tmp/a.txt"}},
        {"event_type": "COMMAND_RUN", "payload": {"command": "pytest"}},
    ]
    script = (
        fn
        + "\nvar __events = "
        + json.dumps(events)
        + ";\nconsole.log(JSON.stringify(countByType(__events, 'COMMAND_RUN')));"
    )
    assert json.loads(_run_node(script)) == 2
    out0 = _run_node(fn + "\nconsole.log(JSON.stringify(countByType([], 'COMMAND_RUN')));")
    assert json.loads(out0) == 0


def test_pick_test_count_fallback_chain():
    """pickTestCount: total_tests || total_events || '—' (comportamiento real)."""
    fn = _extract_function(_read("app.js"), "pickTestCount")
    script = (
        fn
        + "\nconsole.log(JSON.stringify(["
        + "pickTestCount({\"total_tests\": 12, \"total_events\": 99}), "
        + "pickTestCount({\"total_events\": 77}), "
        + "pickTestCount({})"
        + "]));"
    )
    assert json.loads(_run_node(script)) == [12, 77, "—"]
