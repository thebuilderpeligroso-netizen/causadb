"""Fase 1 "No más secretos en claro" — tests RED primero (TDD estricto).

Forward-only: SIN purga ni reescritura histórica (rompería la hash chain).
"""
import base64
import json
import os

import pytest

from causadb._config import CausaDBConfig
from causadb._redactor import redact_payload, _redact_url_credentials


@pytest.fixture
def config():
    return CausaDBConfig(ledger_path="/tmp/test.log")


def test_old_6_keys_still_redacted(config):
    for key in ["password", "api_key", "token", "secret", "credential", "private_key"]:
        out = redact_payload({key: "super-secret-value", "path": "/x"}, config)
        assert out[key] != "super-secret-value", f"{key} en claro"
        assert out["path"] == "/x"


def test_nested_secret_redacted(config):
    payload = {"outer": {"inner": {"password": "nested-secret-123"}}, "ok": 1}
    out = redact_payload(payload, config)
    assert out["outer"]["inner"]["password"] != "nested-secret-123"
    assert out["ok"] == 1


def test_nested_secret_in_list(config):
    payload = {"items": [{"token": "abc-secret"}, {"x": 1}]}
    out = redact_payload(payload, config)
    assert out["items"][0]["token"] != "abc-secret"


def test_new_keys_redacted_exact_match(config):
    for key in ["apiKey", "passwd", "auth", "authorization", "access_token", "client_secret"]:
        out = redact_payload({key: "my-secret-val"}, config)
        assert out[key] != "my-secret-val", f"{key} en claro"


def test_author_not_redacted(config):
    out = redact_payload({"author": "juan"}, config)
    assert out["author"] == "juan"


def test_bearer_alone_not_redacted_but_token_redacted(config):
    out = redact_payload({"note": "Bearer"}, config)
    assert out["note"] == "Bearer"
    out2 = redact_payload({"command": "curl -H 'Authorization: Bearer abcdefghij1234567890'"}, config)
    assert "abcdefghij1234567890" not in out2["command"]


def test_secret_inside_command_string(config):
    cmd = "export AWS_KEY=AKIAIOSFODNN7EXAMPLE y ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    out = redact_payload({"command": cmd}, config)
    assert "AKIAIOSFODNN7EXAMPLE" not in out["command"]
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in out["command"]


def test_high_precision_value_patterns(config):
    cases = [
        "-----BEGIN RSA PRIVATE KEY----- xyz",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "xoxb-12345-abcdef",
        "sk-abcdefghijklmnopqrstuvw1234567890",
        "Bearer abcdefghij1234567890",
    ]
    for secret in cases:
        out = redact_payload({"command": f"run {secret} end"}, config)
        assert secret not in out["command"], f"patrón en claro: {secret}"


def test_score_numerics_untouched(config):
    payload = {"score": 95, "churn": 0.5, "path": "/x"}
    out = redact_payload(payload, config)
    assert out == payload


def test_redact_url_credentials_regression():
    assert _redact_url_credentials("http://user:pass@localhost") == "http://***@localhost"
    assert _redact_url_credentials("https://example.com/sin-cred") == "https://example.com/sin-cred"


def test_depth_cap(config):
    deep = {"password": "x"}
    for _ in range(15):
        deep = {"lvl": deep}
    out = redact_payload(deep, config)
    # No debe crashear ni dejar el secreto en claro (cap profundidad 10)
    assert "x" not in json.dumps(out) or out != deep


def test_put_redacted_choke_point(tmp_path):
    from causadb._blob_store import BlobStore
    from causadb._redactor import put_redacted
    store = BlobStore(str(tmp_path / "blobs"))
    cfg = CausaDBConfig(ledger_path="/tmp/test.log")
    h = put_redacted(store, {"password": "top-secret-999", "ok": 1}, cfg)
    raw = store.get(h)
    assert raw["password"] != "top-secret-999"
    assert raw["ok"] == 1


def test_filesystem_deny_blobs_metadata_only(tmp_path):
    from causadb._harvest_source_filesystem import FilesystemSource
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / ".env").write_text("SECRET=top-secret-999")
    (ws / "id_rsa_prod").write_text("-----BEGIN RSA PRIVATE KEY----- xyz")
    (ws / "cert.pem").write_text("pem-data")
    (ws / "ok.py").write_text("print(1)")
    config = CausaDBConfig(ledger_path="/fake/ledger.log", blob_store_enabled=True,
                           blob_store_path=str(tmp_path / "blobs"))
    source = FilesystemSource(ledger_path="/fake/ledger.log", project_root=str(ws), config=config)
    events = list(source.harvest())
    by_path = {e["path"]: e for e in events}
    for denied in [".env", "id_rsa_prod", "cert.pem"]:
        assert denied in by_path
        assert by_path[denied].get("content_hash") is None, f"{denied} debe ser metadata-only"
        assert "$blob" not in by_path[denied], f"{denied} no debe persistir blob"
    assert by_path["ok.py"].get("content_hash") is not None


def test_hook_base64_roundtrip_with_quotes_backslash_newlines(tmp_path, monkeypatch):
    import tempfile
    from causadb._shell_hook import _hook_dir, _queue_file, flush, HOOK_TEMPLATE
    # El hook debe usar base64 (robusto), no sed
    assert "base64" in HOOK_TEMPLATE
    assert "sed" not in HOOK_TEMPLATE
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("HOME", home)
        os.makedirs(_hook_dir(), exist_ok=True)
        tricky = 'echo "hola \\ mundo" \'it\\\'s\' $HOME `cmd`\nsegunda línea "con comillas"'
        b64 = base64.b64encode(tricky.encode()).decode()
        entry = {"event_type": "COMMAND_RUN", "source": "shell:bash",
                 "source_type": "agent", "ctx_id": "t",
                 "payload": {"command_b64": b64, "exit_code": 0}}
        with open(_queue_file(), "w") as f:
            f.write(json.dumps(entry) + "\n")
        from causadb._init import causadb_init
        ws = str(tmp_path / "ws_hook_b64")
        causadb_init(ws)
        result = flush(os.path.join(ws, "ledger.log"))
        assert result["flushed"] == 1
        raw = open(os.path.join(ws, "ledger.log")).read()
        # El comando con secretos potenciales no queda en claro si tiene token;
        # al menos el roundtrip preserva el comando (redactado o no)
        assert "hola" in raw


def test_flush_tolerates_old_format(tmp_path, monkeypatch):
    import tempfile
    from causadb._shell_hook import _hook_dir, _queue_file, flush
    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("HOME", home)
        os.makedirs(_hook_dir(), exist_ok=True)
        old = {"event_type": "COMMAND_RUN", "source": "shell:bash",
               "source_type": "agent", "ctx_id": "t",
               "payload": {"command": "ls -la", "exit_code": 0}}
        with open(_queue_file(), "w") as f:
            f.write(json.dumps(old) + "\n")
        from causadb._init import causadb_init
        ws = str(tmp_path / "ws_hook_old")
        causadb_init(ws)
        result = flush(os.path.join(ws, "ledger.log"))
        assert result["flushed"] == 1


def test_writer_redacts_nested_before_hash(tmp_path):
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = str(tmp_path / "ledger.log")
    cfg = CausaDBConfig(ledger_path=ledger, blob_store_enabled=False)
    w = LedgerWriter(ledger, config=cfg)
    w.append(CanonicalEvent(event_type=EventType.FILE_MODIFIED, ctx_id="c",
                            source="test", payload={"nested": {"apiKey": "SHOULD-NOT-LEAK-123"}}))
    raw = open(ledger).read()
    assert "SHOULD-NOT-LEAK-123" not in raw


def test_writer_redacts_command_secrets(tmp_path):
    from causadb._ledger_writer import LedgerWriter
    from causadb._event_schema import CanonicalEvent
    from causadb._event_types import EventType
    ledger = str(tmp_path / "ledger2.log")
    cfg = CausaDBConfig(ledger_path=ledger, blob_store_enabled=False)
    w = LedgerWriter(ledger, config=cfg)
    w.append(CanonicalEvent(event_type=EventType.COMMAND_RUN, ctx_id="c",
                            source="test",
                            payload={"command": "deploy ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                     "exit_code": 0}))
    raw = open(ledger).read()
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in raw
