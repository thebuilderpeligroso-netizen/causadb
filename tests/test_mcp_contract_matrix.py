import json
from pathlib import Path

import pytest


MATRIX_PATH = Path(__file__).parents[1] / "docs" / "mcp_contract_matrix.json"
FIELDS = {"client", "contract_version", "format", "canonical_path",
          "handshake_evidence", "status", "last_verified"}
CONFIRMED = {"opencode", "gemini-cli", "claude-code", "codex-cli", "cursor", "windsurf"}
NEGATIVE = {"aider"}
OUT_OF_SCOPE = {"grok", "hermes", "openjarvis", "devin"}


def _contracts():
    return json.loads(MATRIX_PATH.read_text())


def test_matrix_has_exact_schema_and_all_closed_clients():
    data = _contracts()
    assert set(data) == {"contracts"}
    entries = data["contracts"]
    assert {entry["client"] for entry in entries} == CONFIRMED | NEGATIVE | OUT_OF_SCOPE
    for entry in entries:
        assert set(entry) == FIELDS
        assert entry["status"] in {"confirmed", "negative", "out_of_scope"}
        assert entry["contract_version"]
        assert entry["format"]
        if entry["status"] != "out_of_scope":
            assert entry["canonical_path"]
        assert entry["handshake_evidence"]
        assert entry["last_verified"]


@pytest.mark.parametrize("client", sorted(CONFIRMED))
def test_confirmed_clients_have_handshake_evidence(client):
    entry = next(e for e in _contracts()["contracts"] if e["client"] == client)
    assert entry["status"] == "confirmed"
    assert "test" in entry["handshake_evidence"].lower() or "chronicle" in entry["handshake_evidence"].lower()


def test_negative_and_out_of_scope_are_not_confirmed():
    entries = {e["client"]: e for e in _contracts()["contracts"]}
    assert entries["aider"]["status"] == "negative"
    assert all(entries[c]["status"] == "out_of_scope" for c in OUT_OF_SCOPE)
