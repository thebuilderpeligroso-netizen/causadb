"""TDD RED→GREEN: colapso de warnings no_snapshots_for_*.

Spec auditada:
- helper `_collapse_warnings` vive en `causadb/_score.py` (ambos CLIs lo importan).
- Solo toca render md/terminal/markdown de revive y score;
  `compute_score()["warnings"]` y `--format json` BYTE-IDENTICOS.
- Formato: `N eventos sin snapshot (no_snapshots_for_*) — ver JSON para event_ids`
  + `other` verbatim.
- Usarlo en `causadb/cli/_cmd_revive.py:607-612` y
  `causadb/cli/_cmd_score.py:86-91,129-133`.

Tests NUEVOS: mismo input mezcla colapsa igual en revive y score-md/terminal
+ JSON intacto.
"""
import copy
import json

COLLAPSED = "2 eventos sin snapshot (no_snapshots_for_*) — ver JSON para event_ids"
MIX = [
    "ctx-1:no_snapshots_for_evtAAA",
    "ctx-1:no_snapshots_for_evtBBB",
    "survival_defaulted_to_1_no_git_audit",
]


def _fake_score_result():
    return {
        "overall_score": 90.0,
        "churn_score": 90.0,
        "waste_score": 95.0,
        "survival_score": 100.0,
        "weights_used": {"churn": 0.3, "waste": 0.3, "survival": 0.4},
        "correlation_method": "timestamp_proximity",
        "warnings": list(MIX),
        "per_session": {},
    }


def test_collapse_helper_collapses_mixed():
    from causadb._score import _collapse_warnings

    n, other = _collapse_warnings(list(MIX))
    assert n == 2
    assert other == ["survival_defaulted_to_1_no_git_audit"]


def test_score_md_collapses_mixed():
    from causadb.cli._cmd_score import _render_markdown

    md = _render_markdown(_fake_score_result())
    assert COLLAPSED in md
    assert "survival_defaulted_to_1_no_git_audit" in md
    assert "no_snapshots_for_evtAAA" not in md
    assert "no_snapshots_for_evtBBB" not in md


def test_score_terminal_collapses_mixed():
    from causadb.cli._cmd_score import _render_terminal

    out = _render_terminal(_fake_score_result())
    assert COLLAPSED in out
    assert "survival_defaulted_to_1_no_git_audit" in out
    assert "no_snapshots_for_evtAAA" not in out
    assert "no_snapshots_for_evtBBB" not in out


def test_revive_markdown_collapses_same_as_score():
    from causadb.cli._cmd_revive import _generate_revive_markdown
    from causadb.cli._cmd_score import _render_markdown, _render_terminal

    data = {
        "ledger_path": "x",
        "resume": {},
        "governance_decisions": [],
        "score": _fake_score_result(),
    }
    md_rev = _generate_revive_markdown(data)
    assert COLLAPSED in md_rev
    assert "survival_defaulted_to_1_no_git_audit" in md_rev
    assert "no_snapshots_for_evtAAA" not in md_rev
    # mismo input mezcla colapsa igual en revive y score-md/terminal
    assert COLLAPSED in _render_markdown(_fake_score_result())
    assert COLLAPSED in _render_terminal(_fake_score_result())


def test_json_intacto_warnings_byte_identicos():
    from causadb.cli._cmd_score import _render_markdown

    result = _fake_score_result()
    before = copy.deepcopy(result["warnings"])
    _render_markdown(result)
    # el render no debe mutar warnings (JSON sale del dict intacto)
    assert result["warnings"] == before
    # el JSON lleva los event_ids completos (sin colapsar)
    payload = json.dumps(result, sort_keys=True, default=str)
    assert "no_snapshots_for_evtAAA" in payload
    assert "no_snapshots_for_evtBBB" in payload
    assert "survival_defaulted_to_1_no_git_audit" in payload
